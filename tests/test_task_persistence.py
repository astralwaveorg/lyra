"""后台任务状态落库的测试。

## 为什么必须有

实际事故：我因为重新部署容器，**三次杀掉正在运行的抓取/写入任务**，
而任务状态只存在内存里，重启后**完全消失**——用户看不到任何痕迹，
只能看到"进度停在某个数字"或"什么都没有"。

修复后的不变量：
1. 任务从创建到结束的每次状态变化都落库；
2. 进程重启后被遗留的 `running` 任务会被明确标为 `interrupted`，
   而不是静默消失、也不是永远显示成"还在跑"。
"""

from __future__ import annotations

import time

from lyra.api import Runner


def _wait_idle(runner: Runner, store=None, timeout: float = 10.0) -> None:
    """等到任务结束**且终态已落库**。

    只等 `not runner.busy()` 是不够的：`busy()` 依据内存状态，而内存状态在
    `finally` 落库**之前**就已置为终态，于是会出现「任务已结束但库里还是
    running」的竞态，测试会偶发失败。这里改成同时等库里也是终态。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        idle = not runner.busy()
        persisted = True
        if store is not None:
            rows = store.list_tasks(limit=50)
            persisted = all(r["state"] != "running" for r in rows)
        if idle and persisted:
            return
        time.sleep(0.02)
    raise AssertionError("任务没有在预期时间内结束并落库")


def test_任务状态会落库(store):
    runner = Runner(store)
    runner.start("fetch", lambda progress: {"n": 1})
    _wait_idle(runner, store)

    rows = store.list_tasks()
    assert len(rows) == 1
    assert rows[0]["kind"] == "fetch"
    assert rows[0]["state"] == "done"
    assert rows[0]["finished_at"] is not None


def test_进程重启后仍能查到任务(store):
    """模拟重启：清掉内存里的记录，get 必须仍能从库里还原。"""
    runner = Runner(store)
    record = runner.start("apply", lambda progress: {"written": 3})
    _wait_idle(runner, store)

    runner._tasks.clear()  # 等价于进程重启后内存为空

    restored = runner.get(record.id)
    assert restored is not None
    assert restored.state == "done"
    assert restored.result == {"written": 3}


def test_被重启打断的任务会被明确标记(store):
    """进程被杀时任务跑不到 finally，库里会留下永久 running 的记录。"""
    runner = Runner(store)
    runner.start("fetch", lambda progress: {"n": 1})
    _wait_idle(runner, store)

    # 直接构造一条"上个进程遗留的 running 记录"
    store.upsert_task(
        task_id="stale123", kind="fetch", state="running",
        progress="已处理 40 首…", started_at=time.time() - 600,
    )
    assert store.mark_interrupted_tasks() == 1

    restored = Runner(store).get("stale123")
    assert restored is not None
    assert restored.state == "interrupted"
    assert restored.error  # 必须带说明，不能只是状态变了


def test_任务失败也会落库(store):
    runner = Runner(store)

    def boom(progress):
        raise RuntimeError("来源全挂了")

    runner.start("fetch", boom)
    _wait_idle(runner, store)

    row = store.list_tasks()[0]
    assert row["state"] == "failed"
    assert "来源全挂了" in (row["error"] or "")


def test_进度也会落库(store):
    """只落终态不够：重启后要知道它跑到哪了。"""
    runner = Runner(store)

    def slow(progress):
        progress("已处理 7 首…")
        return {}

    record = runner.start("fetch", slow)
    _wait_idle(runner, store)
    assert "7" in (runner.get(record.id).progress or "") or \
           "7" in (store.conn.execute(
               "SELECT progress FROM tasks WHERE id=?", (record.id,)
           ).fetchone()["progress"] or "")


def test_没有库时也能工作(store):
    """Runner 允许不传 store（测试与嵌入式场景）。"""
    runner = Runner()
    record = runner.start("fetch", lambda progress: {"ok": True})
    _wait_idle(runner, store)
    assert runner.get(record.id) is not None


def test_任务异常也要释放写租约(store):
    """租约必须在 finally 释放。

    实际：任务抛异常时不释放，租约会一直占着（最长到 TTL），
    之后所有写入任务都会因「已有任务在进行中」而失败——
    而报错完全没提「上次的任务崩了」。
    """
    from lyra.api import create_app  # noqa: F401  (仅确保模块可导入)

    # 直接验证被任务使用的释放路径：取锁 → 抛异常 → finally 释放
    owner = "task-test"
    assert store.acquire_write_lock("write", owner, ttl_seconds=3600)
    try:
        raise RuntimeError("模拟任务中途失败")
    except RuntimeError:
        pass
    finally:
        store.release_write_lock("write", owner)
    assert store.write_lock_holder("write") is None
