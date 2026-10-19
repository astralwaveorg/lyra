"""写入批次的失败隔离测试。

## 为什么必须有这些测试

实际事故：「一键写入全部」在第 46 首撞上 `database is locked`，
**整个任务直接 failed，后面 70 多首全部没写**。
一个瞬时错误葬送一整批——这是可靠性的核心缺陷，不是参数问题。

修复后的两条不变量，本文件逐条固定：

1. **单文件失败只影响那一首**，其余照常推进；
2. **因锁失败的条目保持 `staged` 而不是 `failed`**——标 failed 会让它永远
   不再重试，保持 staged 才能让下一轮批量写入自动续跑。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lyra import pipeline
from lyra.config import Config, RootConfig, StorageConfig
from lyra.models import RootRole


def _setup(tmp_path: Path, store, flac_factory, names: list[str]):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in names:
        flac_factory(lib / name)

    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    root_id = store.upsert_root(name="lib", role="library", path=str(lib))
    for name in names:
        stat = (lib / name).stat()
        location_id = store.upsert_location(
            root_id=root_id, rel_path=name, size=stat.st_size, mtime=stat.st_mtime
        )
        store.stage_lyrics(
            location_id=location_id,
            content_hash=f"hash-{name}",
            content="[00:01.00]测试歌词\n",
            provider="qq",
            provider_id=f"mid-{name}",
            score=0.9,
            duration_delta=0.1,
            origin="auto",
        )
    return cfg, lib


def _states(store) -> dict[str, str]:
    rows = store.conn.execute(
        "SELECT l.rel_path, s.state FROM staged_lyrics s "
        "JOIN locations l ON l.id = s.location_id"
    ).fetchall()
    return {r["rel_path"]: r["state"] for r in rows}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """去掉重试退避的等待，让测试保持毫秒级。"""
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)


def test_单文件锁错误不终止整批(tmp_path, store, flac_factory, monkeypatch):
    names = ["a.flac", "b.flac", "c.flac"]
    cfg, lib = _setup(tmp_path, store, flac_factory, names)

    seen: list[str] = []

    def fake_apply_one(_store, _cfg, _base, _backup, row, summary):
        seen.append(row["rel_path"])
        if row["rel_path"] == "b.flac":
            raise sqlite3.OperationalError("database is locked")
        _store.set_staged_state(row["id"], "applied")
        return True

    monkeypatch.setattr(pipeline, "_apply_one", fake_apply_one)

    summary = pipeline.run_apply(store, cfg, cfg.roots[0])

    # a 先处理，c **在处理完 b 之后仍然被执行**——这正是「不终止整批」的含义
    assert seen[0] == "a.flac"
    assert seen[-1] == "c.flac", "c 不该被 b 的失败牵连"
    # b 会被重试（首次 + 3 次退避重试），重试用尽才降级为「稍后重试」
    assert seen.count("b.flac") == 4, f"b 应重试 4 次，实际 {seen.count('b.flac')}"
    assert summary.applied == 2
    assert summary.deferred_locked == 1
    assert summary.apply_failed == 0


def test_锁错误保留待写入状态而不是标记失败(tmp_path, store, flac_factory, monkeypatch):
    """标成 failed 会让这首永远不再重试；必须保持 staged 才能续跑。"""
    names = ["a.flac", "b.flac"]
    cfg, _lib = _setup(tmp_path, store, flac_factory, names)

    def fake_apply_one(_store, _cfg, _base, _backup, row, summary):
        if row["rel_path"] == "a.flac":
            raise sqlite3.OperationalError("database is locked")
        _store.set_staged_state(row["id"], "applied")
        return True

    monkeypatch.setattr(pipeline, "_apply_one", fake_apply_one)
    pipeline.run_apply(store, cfg, cfg.roots[0])

    states = _states(store)
    assert states["a.flac"] == "staged", "锁失败必须保持 staged 以便下轮续跑"
    assert states["b.flac"] == "applied"


def test_非锁错误只标记该文件失败(tmp_path, store, flac_factory, monkeypatch):
    names = ["a.flac", "b.flac", "c.flac"]
    cfg, _lib = _setup(tmp_path, store, flac_factory, names)

    def fake_apply_one(_store, _cfg, _base, _backup, row, summary):
        if row["rel_path"] == "b.flac":
            raise ValueError("音频结构不支持")
        _store.set_staged_state(row["id"], "applied")
        return True

    monkeypatch.setattr(pipeline, "_apply_one", fake_apply_one)
    summary = pipeline.run_apply(store, cfg, cfg.roots[0])

    states = _states(store)
    assert states["a.flac"] == "applied"
    assert states["b.flac"] == "failed"
    assert states["c.flac"] == "applied", "c 不该被 b 的失败牵连"
    assert summary.apply_failed == 1
    assert summary.deferred_locked == 0


def test_锁错误会重试而不是一次就放弃(tmp_path, store, flac_factory, monkeypatch):
    """瞬时争用应该靠重试消化，而不是立刻降级为「稍后重试」。"""
    names = ["a.flac"]
    cfg, _lib = _setup(tmp_path, store, flac_factory, names)

    attempts = {"n": 0}

    def flaky(_store, _cfg, _base, _backup, row, summary):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        _store.set_staged_state(row["id"], "applied")
        return True

    monkeypatch.setattr(pipeline, "_apply_one", flaky)
    summary = pipeline.run_apply(store, cfg, cfg.roots[0])

    assert attempts["n"] == 3
    assert summary.applied == 1
    assert summary.deferred_locked == 0
