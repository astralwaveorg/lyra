"""清理被更优候选取代的待复核记录。

## 为什么需要

实际发现：同一首曲目**已经用 0.910 的候选写入过**，却仍有一条 0.686 的旧
`needs_review` 记录挂在队列里。抽查 10 条「缺判定证据」的记录，全是这种情况。

对使用者是纯噪声——他以为这首歌还需要决定，实际早就写好了。
队列里混着这种条目，会让人不再信任队列本身。
"""

from __future__ import annotations

import time

import pytest

from lyra.db import open_store


@pytest.fixture
def store_with_location(tmp_path):
    store = open_store(tmp_path / "d")
    root_id = store.upsert_root(name="r", role="library", path=str(tmp_path / "lib"))
    location_id = store.upsert_location(
        root_id=root_id, rel_path="a.flac", size=1, mtime=1.0
    )
    yield store, location_id
    store.close()


def _stage(store, location_id, pid, score, state):
    store.conn.execute(
        "INSERT INTO staged_lyrics(location_id, content_hash, content, provider,"
        " provider_id, score, duration_delta, state, created_at, origin)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (location_id, f"h{pid}", "x" * 24, "qq", pid, score, 0.0, state,
         time.time(), "auto"),
    )
    store.conn.commit()


def test_低分复核被已写入的更优候选取代(store_with_location):
    store, location_id = store_with_location
    _stage(store, location_id, "good", 0.91, "applied")
    _stage(store, location_id, "stale", 0.69, "needs_review")

    assert store.supersede_obsolete_reviews() == 1

    states = {
        r["provider_id"]: r["state"]
        for r in store.conn.execute("SELECT provider_id, state FROM staged_lyrics")
    }
    assert states["good"] == "applied", "更优的那条不该被动到"
    assert states["stale"] == "superseded"


def test_同分不清理(store_with_location):
    """只清理**严格劣于**已有记录的。同分可能是不同来源的合理候选。"""
    store, location_id = store_with_location
    _stage(store, location_id, "a", 0.80, "applied")
    _stage(store, location_id, "b", 0.80, "needs_review")

    assert store.supersede_obsolete_reviews() == 0


def test_高分复核不被低分已写入取代(store_with_location):
    """反过来不成立：待复核的分更高，说明它可能是更好的选择，要留给人判断。"""
    store, location_id = store_with_location
    _stage(store, location_id, "low", 0.60, "applied")
    _stage(store, location_id, "high", 0.90, "needs_review")

    assert store.supersede_obsolete_reviews() == 0


def test_没有更优记录时不动(store_with_location):
    store, location_id = store_with_location
    _stage(store, location_id, "only", 0.70, "needs_review")
    assert store.supersede_obsolete_reviews() == 0
