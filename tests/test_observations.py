"""歌词观察表的写入语义：**状态变化才记一条**。

## 为什么必须有

监控每 30 秒做一次整库对账（204 首约 1 秒，可接受），但对账会把每个文件的
观察结果写一遍。如果照收不误，这张表每天增长约 58 万行，而其中几乎全部与
上一条一字不差——实际 6 小时就积了 15 万行。

表的意义是「状态变化史」，不是心跳。所以：
1. 与上一条完全相同的观察不再插入（返回上一条的 id）；
2. 任何字段变了都要插入（否则界面读到的是旧状态，比不记更糟）；
3. `prune_observations` 能清掉旧版本留下的重复行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.db import open_store


@pytest.fixture
def env(tmp_path: Path):
    s = open_store(tmp_path / "data")
    root_id = s.upsert_root(name="lib", role="library", path=str(tmp_path / "lib"))
    location_id = s.upsert_location(
        root_id=root_id, rel_path="a.flac", size=1, mtime=1.0,
    )
    yield s, location_id
    s.close()


def _count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM lyric_observations").fetchone()[0]


def _record(store, location_id, **over):
    kwargs = dict(
        location_id=location_id,
        embedded_state="synced",
        embedded_fields=("LYRICS",),
        embedded_hash="h1",
        sidecar_state="synced",
        sidecar_hash="h1",
        sidecar_size=100,
        sidecar_path="/lib/a.lrc",
        sidecar_names_differ=False,
        has_embedded_cover=True,
        issues=(),
    )
    kwargs.update(over)
    return store.record_lyric_observation(**kwargs)


def test_相同观察不重复入库(env):
    store, location_id = env
    first = _record(store, location_id)
    second = _record(store, location_id)
    third = _record(store, location_id)
    assert _count(store) == 1, "完全相同的观察不该每次都插一行"
    assert first == second == third, "返回的应当是同一个观察 id"


def test_状态变化必须记一条(env):
    store, location_id = env
    _record(store, location_id)
    _record(store, location_id, embedded_state="absent")
    assert _count(store) == 2


def test_缺陷列表变化也必须记一条(env):
    store, location_id = env
    _record(store, location_id)
    _record(store, location_id, issues=("missing_mbid",))
    assert _count(store) == 2
    latest = store.conn.execute(
        "SELECT issues FROM lyric_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "missing_mbid" in latest["issues"]


def test_清理只保留每首歌最近几条(env):
    store, location_id = env
    for i in range(6):
        _record(store, location_id, embedded_hash=f"h{i}")
    assert _count(store) == 6
    removed = store.prune_observations(keep_per_location=3)
    assert removed == 3
    assert _count(store) == 3
    kept = [r["embedded_hash"] for r in store.conn.execute(
        "SELECT embedded_hash FROM lyric_observations ORDER BY id")]
    assert kept == ["h3", "h4", "h5"], "必须保留最新的几条，而不是随便三条"
