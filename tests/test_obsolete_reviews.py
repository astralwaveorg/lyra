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


def _observe(store, location_id, embedded_state="synced", sidecar_state="synced"):
    """写一条「此刻文件里有什么」的观测。"""
    store.conn.execute(
        "INSERT INTO lyric_observations(location_id, observed_at, embedded_state,"
        " sidecar_state, sidecar_path, issues)"
        " VALUES(?,?,?,?,?,?)",
        (location_id, time.time(), embedded_state, sidecar_state, None, "[]"),
    )
    store.conn.commit()


def test_已经有歌词的曲目不再留在复核队列(store_with_location):
    """实际：曲库整理完之后，队列里 58 首有 67 条属于「早就写好了」。

    使用者看到的是「这 58 首还要我决定」，实际其中绝大多数不需要——
    队列一旦掺了这种噪声，人就不再看它了。
    """
    store, location_id = store_with_location
    _stage(store, location_id, "good", 0.91, "applied")
    _stage(store, location_id, "stale", 0.69, "needs_review")
    _observe(store, location_id)

    assert store.supersede_obsolete_reviews() == 1

    states = {
        r["provider_id"]: r["state"]
        for r in store.conn.execute("SELECT provider_id, state FROM staged_lyrics")
    }
    assert states["good"] == "applied", "已经写入的那条不该被动到"
    assert states["stale"] == "superseded"


def test_只有占位文案的曲目保留候选(store_with_location):
    """占位不是「没有歌词」的证据——真歌词仍可能把它换掉，候选要留着。"""
    store, location_id = store_with_location
    _stage(store, location_id, "ph", None, "applied")
    _stage(store, location_id, "cand", 0.80, "needs_review")
    _observe(store, location_id, embedded_state="placeholder", sidecar_state="placeholder")

    assert store.supersede_obsolete_reviews() == 0


def test_时间轴异常的曲目保留候选(store_with_location):
    """候选可能正是修好「时间轴异常」的那一条。"""
    store, location_id = store_with_location
    _stage(store, location_id, "bad", None, "applied")
    _stage(store, location_id, "cand", 0.80, "needs_review")
    _observe(store, location_id, embedded_state="synced_suspicious",
             sidecar_state="synced_suspicious")

    assert store.supersede_obsolete_reviews() == 0


def test_还没有歌词时不动(store_with_location):
    store, location_id = store_with_location
    _stage(store, location_id, "only", 0.70, "needs_review")
    _observe(store, location_id, embedded_state="absent", sidecar_state="absent")
    assert store.supersede_obsolete_reviews() == 0
