"""待办计数、列表、实际可处理三者必须一致。

## 线上实测的三个问题

1. **歌词计数 63、列表 94**：计数排除了文件已删的位置，列表没排；
   前端还传 `exclude_safe=true` 把推荐条目藏掉（页签已删，条目凭空
   消失）——两把刀一起砍，界面上没有一个数是对的。
2. **标签 134 条 auto 没写入**：自动写入的 sweep 先拿写租约、再调
   `_write_auto`（它内部自己会拿）——自己等自己，静默写 0 条。
3. **文件删了提案还在**：review 421 条里 169 条对应的文件已删（40%），
   全是死条目，没有任何处理入口。

本文件固定：列表与计数同口径、sweep 能真的写入、缺失位置不再生成
提案、失败条目自动重试。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store


def _client(tmp_path: Path) -> tuple[TestClient, object, int]:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    return TestClient(create_app(cfg)), store, root_id


def test_歌词列表与计数同口径(tmp_path: Path):
    """计数排除文件已删的条目，列表必须同样排除。"""
    client, store, root_id = _client(tmp_path)
    for rel in ("活的.flac", "已删.flac"):
        loc = store.upsert_location(
            root_id=root_id, rel_path=rel, size=1, mtime=1.0,
        )
        store.stage_lyrics(
            location_id=int(loc), content_hash="h" + rel,
            content="[00:01.00]词\n", provider="qq", state="needs_review",
        )
    store.mark_location_missing(
        int(store.conn.execute(
            "SELECT id FROM locations WHERE rel_path='已删.flac'"
        ).fetchone()["id"])
    )

    counts = client.get("/api/v1/todo").json()["counts"]
    assert counts["lyrics"] == 1, "计数只算文件还在的"
    page = client.get("/api/v1/staged", params={"state": "needs_review"}).json()
    rels = {item["rel_path"] for item in page["items"]}
    assert rels == {"活的.flac"}, f"列表也不该出现已删文件：{rels}"
    assert page["total"] == counts["lyrics"], "列表 total 与计数一致"
    store.close()


def test_缺失位置不再生成提案(tmp_path: Path):
    """upsert 对已缺失的位置直接返回，不再制造死提案。"""
    client, store, root_id = _client(tmp_path)
    loc = store.upsert_location(
        root_id=root_id, rel_path="已删.flac", size=1, mtime=1.0,
    )
    store.mark_location_missing(int(loc))
    pid = store.upsert_tag_proposal(
        location_id=int(loc), state="review", source="search", confidence=0.8,
        title="歌", artists="人", payload={"rows": []},
    )
    assert pid == -1, "缺失位置不应生成提案"
    n = store.conn.execute(
        "SELECT COUNT(*) AS n FROM tag_proposals WHERE location_id=?", (int(loc),)
    ).fetchone()["n"]
    assert n == 0
    store.close()


def test_歌词失败条目可重试(tmp_path: Path):
    """state='failed' 的条目重置为 staged 后会被写入流程再次处理。"""
    client, store, root_id = _client(tmp_path)
    loc = store.upsert_location(
        root_id=root_id, rel_path="歌.flac", size=1, mtime=1.0,
    )
    sid = store.stage_lyrics(
        location_id=int(loc), content_hash="h1", content="[00:01.00]词\n",
        provider="qq", state="failed",
    )
    store.set_staged_state(int(sid), "staged")
    row = store.conn.execute(
        "SELECT state FROM staged_lyrics WHERE id=?", (int(sid),)
    ).fetchone()
    assert row["state"] == "staged", "重试后应回到待写入"
    store.close()
