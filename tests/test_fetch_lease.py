"""抓取任务不得占用写租约（2.2.2 回归）。

实际故障：纯抓取任务（apply_now=false，只把歌词暂存进数据库、不碰音乐
文件）也去申请写租约，并按 TTL 持有 15 分钟。它一开跑，人工点「开始写入」
就拿不到许可——用户看到的是「任务已开始，但一直没有写入」。

抓取与写入的职责必须分开：只有真正要写文件的任务才申请写租约。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store


def _cfg(tmp_path: Path) -> Config:
    lib = tmp_path / "lib"
    make_flac(lib / "a.flac", entries=(("TITLE", "歌"), ("ARTIST", "人")))
    cfg = Config(
        roots=[RootConfig(name="main", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.write_enabled = True
    return cfg


def test_纯抓取不申请写租约(tmp_path: Path):
    """抓取（不写入）期间不应出现写租约。"""
    from lyra.pipeline import run_fetch

    cfg = _cfg(tmp_path)
    store = open_store(cfg.storage.resolved_data_dir)

    class _FakeProvider:
        name = "qq"

        def search(self, _query):  # noqa: ANN001, ANN202
            return []

        def lyrics(self, _c):  # noqa: ANN001, ANN202
            return None

    from lyra.audit import audit_root

    run_fetch(
        store, cfg, cfg.roots[0], [_FakeProvider()],
        audit=audit_root(cfg.roots[0]),
    )
    locks = store.conn.execute("SELECT name FROM write_locks").fetchall()
    assert not locks, "抓取不应申请写租约"
    store.close()


def test_写入前先检查租约是否可用(tmp_path: Path):
    """有别人占着租约时，等待应在有限时间内结束并如实报告。"""
    cfg = _cfg(tmp_path)
    store = open_store(cfg.storage.resolved_data_dir)
    assert store.acquire_write_lock("write", "other", ttl_seconds=60, wait_seconds=0.0)
    got = store.acquire_write_lock(
        "write", "me", ttl_seconds=60, wait_seconds=0.2
    )
    assert got is False
    assert store.write_lock_holder("write") == "other"
    store.close()
