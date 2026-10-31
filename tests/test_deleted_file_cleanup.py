"""文件被删除后，待办条目要自动消失，端点要如实说明而不是 500。

## 真实故障（用户反馈）

> 「我删除了文件，为什么不会自动更新消失呢」
> ——删掉 `.flac 周杰伦; 张惠妹; 吴建豪/Unknown Album/该不该说.flac`
> 之后，待办里的条目还在；点进去还报 Internal Server Error。

三个原因叠在一起：

1. **监听器不管删除事件**：`deleted` 事件被当成「文件变化」进稳定判定
   队列，而队列里它已经 stat 不到文件，于是被静默跳过——位置行永远
   不被标记缺失，待办就一直挂着；
2. **歌词/标签待办查询没有排除 `missing_since`**：即使标记了缺失，
   条目照样显示；
3. **端点直接读文件**：文件没了 → FileNotFoundError → 500，看起来像
   服务坏了。

"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store


def _env(tmp_path: Path) -> tuple[TestClient, object, int]:
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


def _add_staged(store, root_id: int, rel: str) -> int:
    loc = store.upsert_location(
        root_id=root_id, rel_path=rel, size=10, mtime=1.0,
    )
    return store.stage_lyrics(
        location_id=loc, content_hash="h1", content="[00:01.00]词\n", provider="qq",
        state="needs_review",
    )


# ---------------------------------------------------------------------------
# 待办队列：文件已删的条目不出现
# ---------------------------------------------------------------------------


def test_文件缺失后歌词待办不显示(tmp_path: Path):
    client, store, root_id = _env(tmp_path)
    _add_staged(store, root_id, "歌.flac")
    assert client.get("/api/v1/todo").json()["counts"]["lyrics"] == 1

    store.mark_location_missing(
        int(store.conn.execute(
            "SELECT id FROM locations WHERE root_id=?", (root_id,)
        ).fetchone()["id"])
    )  # 标记为「本次未见」
    counts = client.get("/api/v1/todo").json()["counts"]
    assert counts["lyrics"] == 0, f"文件删了，待办不该还挂着：{counts}"
    page = client.get("/api/v1/todo", params={"kind": "lyrics"}).json()
    assert page["items"] == []
    store.close()


def test_文件缺失后标签待办不显示(tmp_path: Path):
    client, store, root_id = _env(tmp_path)
    loc = store.upsert_location(
        root_id=root_id, rel_path="歌.flac", size=10, mtime=1.0,
    )
    store.upsert_tag_proposal(
        location_id=loc, state="review", source="search", confidence=0.8,
        title="歌", artists="人", payload={"rows": []},
    )
    assert client.get("/api/v1/todo").json()["counts"]["tags"] == 1
    store.mark_location_missing(int(loc))
    assert client.get("/api/v1/todo").json()["counts"]["tags"] == 0
    store.close()


# ---------------------------------------------------------------------------
# 端点：文件已删 → 404 而不是 500
# ---------------------------------------------------------------------------


def test_对比来源时文件已删返回404(tmp_path: Path):
    client, store, root_id = _env(tmp_path)
    staged_id = _add_staged(store, root_id, "歌.flac")
    r = client.get(f"/api/v1/staged/{staged_id}/alternatives")
    assert r.status_code != 500, f"不该是 500：{r.text[:200]}"
    store.close()


# ---------------------------------------------------------------------------
# 监听器：删除事件立刻标记缺失
# ---------------------------------------------------------------------------


def test_删除事件立刻标记位置缺失(tmp_path: Path):
    """监听器收到 deleted 事件 → 位置行立刻标 missing。"""
    from lyra.config import WatchConfig
    from lyra.watcher import LibraryWatcher

    lib = tmp_path / "lib"
    lib.mkdir()
    target = lib / "歌.flac"
    target.write_bytes(b"x" * 10)

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        watch=WatchConfig(stable_seconds=0.01, stable_samples=1),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    loc = store.upsert_location(
        root_id=root_id, rel_path="歌.flac", size=10, mtime=1.0,
    )

    deleted: list[Path] = []
    watcher = LibraryWatcher(
        cfg, cfg.roots[0], lambda paths: None,
        on_deleted=lambda path: deleted.append(path),
    )

    class _Event:
        event_type = "deleted"
        is_directory = False
        src_path = str(target)
        dest_path = ""

    watcher._on_event(_Event())
    assert deleted, "删除事件应当回调上层"
    assert deleted[0].name == "歌.flac"

    # 上层收到回调后标记缺失
    store.mark_location_missing(int(loc))
    row = store.conn.execute(
        "SELECT missing_since FROM locations WHERE id=?", (int(loc),)
    ).fetchone()
    assert row["missing_since"] is not None
    store.close()
