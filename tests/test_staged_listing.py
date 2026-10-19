"""复核队列的「按曲目聚合」不能因为 SQL 联结而重复列出同一条记录。

## 为什么必须有

真实故障（截图可见）：待复核页上同一首歌出现两个一模一样的
「QQ 音乐 0.60」标签，而它们**其实是同一条暂存记录**。

原因：`resolutions` 表的 `(provider, provider_id)` **不唯一**。
实际 `qq/0040T0EA00oi4h` 有两条决议，分别属于《龍捲風》与《存在》——
同一个 QQ 候选被两首不同的歌各解析过一次。原来的写法是

    LEFT JOIN resolutions r ON r.provider = s.provider
                           AND r.provider_id = s.provider_id

一对多联接把一条 staged 行放大成两行。除了重复标签，**其中一次的判定依据
还来自另一首歌**：界面会显示《龍捲風》那次解析留下的理由，而使用者正在
决定要不要给《存在》采用它。理由错、曲目错、而界面看上去一切正常。

修复方式是用相关子查询只取唯一一行，并且优先取**同一 location** 的那一条。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole


def _cfg(tmp_path: Path, lib: Path) -> Config:
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def _insert_resolution(store, *, provider, provider_id, key, location_id, reason):
    now = time.time()
    store.conn.execute(
        "INSERT INTO resolutions(resolution_key, provider, provider_id, outcome, "
        "candidate_json, location_id, attempts, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (key, provider, provider_id, "needs_review",
         # 形状必须与 pipeline 真正写入的一致：判定依据取自
         # `recommendation.text`（其次才用第一道未通过的闸门兜底）。
         json.dumps({"recommendation": {"verdict": "inspect", "text": reason},
                     "checks": [{"key": "artist", "label": "艺人相似度",
                                 "passed": False, "detail": reason}]},
                    ensure_ascii=False),
         location_id, 1, now, now),
    )
    store.conn.commit()


@pytest.fixture
def seeded(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    flac_factory(lib / "两张专辑" / "存在.flac",
                 entries=(("TITLE", "存在"), ("ARTIST", "G.E.M.鄧紫棋")))
    cfg = _cfg(tmp_path, lib)

    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False
    )
    # 另一首歌（另一个 location），它的决议用同一个 QQ 候选 id
    other_id = store.upsert_location(
        root_id=root_id, rel_path="两张专辑/龍捲風.flac", size=1, mtime=1.0
    )
    mine_id = store.upsert_location(
        root_id=root_id, rel_path="两张专辑/存在.flac", size=1, mtime=1.0
    )

    staged_id = store.stage_lyrics(
        location_id=mine_id, content_hash="hash-exists", content="[00:01.00]词\n",
        provider="qq", provider_id="0040T0EA00oi4h", score=0.599,
        duration_delta=0.03, state="needs_review",
    )
    # 先插「别的歌」那条，再插本曲目那条：旧写法会同时命中，且顺序不定
    _insert_resolution(store, provider="qq", provider_id="0040T0EA00oi4h",
                       key="other-key", location_id=other_id,
                       reason="来自另一首歌的理由")
    _insert_resolution(store, provider="qq", provider_id="0040T0EA00oi4h",
                       key="mine-key", location_id=mine_id,
                       reason="这条才是本曲目的理由")
    store.close()
    return cfg, staged_id


def test_同一候选有多条决议时只列出一条(seeded):
    cfg, staged_id = seeded
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.get("/api/v1/staged", params={"state": "needs_review"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1, d
    assert len(d["items"]) == 1, f"一条暂存记录被放大成 {len(d['items'])} 行"
    assert [i["id"] for i in d["items"]] == [staged_id]
    # 聚合视图同样不能重复
    assert len(d["groups"]) == 1
    assert len(d["groups"][0]["candidates"]) == 1, d["groups"][0]["candidates"]


def test_理由必须取自同一曲目的那条决议(seeded):
    """这是比「重复」更严重的一半：理由取错会让人按错误依据做决定。"""
    cfg, _staged_id = seeded
    app = create_app(cfg)
    with TestClient(app) as c:
        d = c.get("/api/v1/staged", params={"state": "needs_review"}).json()
    reason = d["items"][0].get("reason") or ""
    assert "本曲目" in reason, f"取到了别的曲目的理由：{reason!r}"


# ---------------------------------------------------------------------------
# 环境差异守卫：容器里的 SQLite 比本机更严
# ---------------------------------------------------------------------------


def test_子查询的_ORDER_BY_不得引用外层列():
    """这条 SQL 的改动必须**在容器里也跑一次**。

    实际：容器里是 SQLite 3.46.1，子查询的 `ORDER BY` 里不允许引用外层列——

        ORDER BY CASE WHEN r2.location_id = s.location_id THEN 0 ELSE 1 END

    会报 `no such column: s.location_id`，而当次请求直接 500。
    **同一个查询在本机 macOS 的 SQLite 上完全正常**，所以单元测试全绿，
    实际运行「待复核」页打不开。凡是依赖 SQLite 版本行为的写法，本机测不出来，
    只能靠这条文本守卫 + 部署后实跑一次。

    现在用的是「两个标量子查询 + COALESCE」：子查询的 ORDER BY 只引用
    自己的别名，任何 SQLite 版本都接受。
    """
    import inspect

    from lyra.api import _staged

    source = inspect.getsource(_staged)
    # 先去掉注释行：这段 SQL 的注释里**故意**引用了那个错误写法当反面例子，
    # 不剥掉注释的话守卫会被自己的说明文字触发。
    sql = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ORDER BY CASE WHEN" not in sql, (
        "子查询 ORDER BY 里引用外层列会在容器 SQLite 上报 no such column"
    )
    assert sql.count("COALESCE(") >= 1, "应当用 COALESCE 表达「优先同 location」"
    # 出现「外层别名 + 点号」出现在子查询 ORDER BY 之后的写法一律可疑。
    # 例外：**聚合**里的外层列（`ORDER BY MAX(s.created_at)`）是顶层聚合排序，
    # 不是子查询排序，SQLite 各版本都接受——按曲目分页那条查询用的就是它。
    for chunk in sql.split("ORDER BY")[1:-1]:
        head = chunk[:80]
        if head.lstrip().startswith(("MAX(", "MIN(", "COUNT(", "SUM(")):
            continue
        assert " s." not in head and "(s." not in head, (
            f"子查询 ORDER BY 附近引用了外层列：{head!r}"
        )


def test_按曲目分页不会把同一首歌拆到两页(tmp_path: Path, flac_factory):
    """逐条分页会把同一首歌的候选切到两页：使用者会以为队列里有两首。

    所以按曲目切页：`total` 是**曲目数**，一页里的曲目候选一次取全。
    """
    from lyra.api import create_app

    lib = tmp_path / "lib"
    cfg = _cfg(tmp_path, lib)
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False)
    # 3 首歌，每首 2 个来源的候选（共 6 条暂存）
    for _index, name in enumerate(("甲", "乙", "丙")):
        location_id = store.upsert_location(
            root_id=root_id, rel_path=f"{name}.flac", size=1, mtime=1.0)
        for provider in ("qq", "lrclib"):
            store.stage_lyrics(
                location_id=location_id, content_hash=f"h-{name}-{provider}",
                content="[00:01.00]词\n", provider=provider,
                provider_id=f"{name}-{provider}", score=0.8,
                duration_delta=0.1, state="needs_review",
            )
    store.close()

    with TestClient(create_app(cfg)) as client:
        first = client.get("/api/v1/staged", params={
            "state": "needs_review", "by_track": "true", "size": 2, "page": 1,
        }).json()
        second = client.get("/api/v1/staged", params={
            "state": "needs_review", "by_track": "true", "size": 2, "page": 2,
        }).json()

    assert first["total"] == 3, "total 应当是曲目数，不是暂存条数"
    assert len(first["groups"]) == 2, "第一页两首歌，各自候选必须齐全"
    assert all(len(g["candidates"]) == 2 for g in first["groups"])
    assert len(second["groups"]) == 1
    paths = [g["rel_path"] for g in first["groups"]] + [
        g["rel_path"] for g in second["groups"]
    ]
    assert len(set(paths)) == 3, f"同一首歌被拆到了两页：{paths}"
