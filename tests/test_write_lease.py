"""跨进程写租约的测试。

## 为什么需要

Web 容器里的批量任务与独立的监控容器**都会全库写入**。同时跑会：
1. 重复请求上游来源；
2. 互相争 SQLite 写锁；
3. 每次把整库跑两遍（实际每次重复浪费约 10 分钟）。

租约让监控发现批量任务在跑就跳过本轮。

关键设计：租约靠 `expires_at` 过期，而不是只靠显式释放——
进程被 kill 时不可能执行释放逻辑，没有过期就会永久卡死后续所有写入。
"""

from __future__ import annotations

import time


def test_同一时刻只有一个持有者(store):
    assert store.acquire_write_lock("write", "task-A")
    assert not store.acquire_write_lock("write", "task-B")
    assert store.write_lock_holder("write") == "task-A"


def test_同一持有者可以重复取锁(store):
    """长任务会重复取锁来续期，不能把自己挡在外面。"""
    assert store.acquire_write_lock("write", "task-A")
    assert store.acquire_write_lock("write", "task-A")
    assert store.write_lock_holder("write") == "task-A"


def test_释放后别人可以取得(store):
    store.acquire_write_lock("write", "task-A")
    store.release_write_lock("write", "task-A")
    assert store.write_lock_holder("write") is None
    assert store.acquire_write_lock("write", "task-B")


def test_过期的锁不会永久卡死(store):
    """进程被 kill 时来不及释放，租约必须能自动过期。"""
    store.acquire_write_lock("write", "dead-task", ttl_seconds=0.05)
    assert store.write_lock_holder("write") == "dead-task"
    time.sleep(0.1)
    assert store.write_lock_holder("write") is None
    assert store.acquire_write_lock("write", "task-B"), "过期锁必须能被抢占"


def test_续租会延长有效期(store):
    store.acquire_write_lock("write", "long-task", ttl_seconds=0.1)
    time.sleep(0.05)
    store.renew_write_lock("write", "long-task", ttl_seconds=10.0)
    time.sleep(0.1)
    assert store.write_lock_holder("write") == "long-task", "续租后不应过期"


def test_别人的续租不影响持有者(store):
    store.acquire_write_lock("write", "task-A", ttl_seconds=0.05)
    store.renew_write_lock("write", "task-B", ttl_seconds=100.0)  # B 并不持有
    time.sleep(0.1)
    assert store.write_lock_holder("write") is None, "非持有者不应能续租"


# ---------------------------------------------------------------------------
# 实际遇到过的两个坑
# ---------------------------------------------------------------------------


def test_重启后清空遗留租约(store):
    """进程被杀时来不及释放，租约会一直占着（最长到 TTL 结束）。

    实际后果：一次任务被重启打断后，**后续所有写入任务都被拒绝**，
    而错误只说「已有任务在进行中」，很难定位到是遗留锁的问题。
    """
    store.acquire_write_lock("write", "被重启杀掉的任务", ttl_seconds=3600)
    assert store.write_lock_holder("write") == "被重启杀掉的任务"

    removed = store.release_all_write_locks()

    assert removed == 1
    assert store.write_lock_holder("write") is None
    assert store.acquire_write_lock("write", "新任务"), "清理后新任务必须能取到锁"


def test_清空租约在没有锁时也安全(store):
    assert store.release_all_write_locks() == 0


def test_监控批处理期间要持有租约(store):
    """只「检查」别人有没有租约是不够的——自己也得持有。

    实际：监控在跑全库刮削的同时，Web 端的写入任务也能开工（因为监控从
    不持有租约，只检查），两边写库互相打断，一次 12 首的写入里 9 首被
    判「数据库持续被占用」而推迟。
    """
    from lyra.cli import _watch_lease

    # 别的任务持有租约时：监控必须让路，且不得把别人的租约顶掉
    assert store.acquire_write_lock("write", "task-A")
    with _watch_lease(store, "library") as got:
        assert got is False
    assert store.write_lock_holder("write") == "task-A", "监控不能抢走别人的租约"

    # 没人持有时：拿到租约，并在退出时释放
    store.release_write_lock("write", "task-A")
    with _watch_lease(store, "library") as got:
        assert got is True
        assert store.write_lock_holder("write") == "watch"
    assert store.write_lock_holder("write") is None, "退出后必须释放"


def test_等待可以等到对方释放(tmp_path):
    """人工操作会**等**租约，而不是直接失败。"""
    from lyra.db import open_store

    store = open_store(tmp_path / "data")
    try:
        assert store.acquire_write_lock("write", "watch", ttl_seconds=60)
        # 另一个人登记等待，然后在后台释放
        import threading
        import time

        def release_later():
            time.sleep(0.3)
            store.release_write_lock("write", "watch")

        t = threading.Thread(target=release_later)
        t.start()
        ok = store.acquire_write_lock("write", "manual", ttl_seconds=60,
                                      wait_seconds=5)
        t.join()
        assert ok is True
    finally:
        store.close()


def test_等待队列登记与清理(tmp_path):
    from lyra.db import open_store

    store = open_store(tmp_path / "data")
    try:
        store.request_write("manual-1")
        assert store.waiting_writers() == ["manual-1"]
        store.cancel_write_request("manual-1")
        assert store.waiting_writers() == []
    finally:
        store.close()


def test_监控看到等待请求就提前收手(tmp_path, flac_factory):
    """`should_yield` 返回 True 时，写入在**当前文件之后**停下。

    这是「人工优先」的落点：监控不能把 50 首写完才让出。
    """
    from lyra.audit import audit_root
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store
    from lyra.models import RootRole
    from lyra.pipeline import run_apply, sync_state

    lib = tmp_path / "lib"
    lib.mkdir()
    paths = []
    for i in range(3):
        p = lib / f"{i:02d}.flac"
        flac_factory(p, entries=(("TITLE", f"t{i}"),))
        paths.append(p)

    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
        for p in paths:
            rows = store.conn.execute(
                "SELECT l.id FROM locations l WHERE l.rel_path=?",
                (p.name,),
            ).fetchone()
            store.stage_lyrics(
                location_id=rows["id"], content_hash="h" + p.name,
                content="[00:01.00]词\n", provider="t", provider_id="x",
                state="staged", origin="auto",
            )
        calls = {"n": 0}

        def should_yield():
            calls["n"] += 1
            return calls["n"] > 1      # 第一首之后就开始让

        summary = run_apply(store, cfg, cfg.roots[0], should_yield=should_yield)
        assert summary.yielded is True
        assert summary.applied == 1, summary
        left = store.conn.execute(
            "SELECT COUNT(*) FROM staged_lyrics WHERE state='staged'"
        ).fetchone()[0]
        assert left == 2, "剩下的必须仍是待写入，不能丢"
    finally:
        store.close()
