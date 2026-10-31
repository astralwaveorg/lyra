"""数据库连接必须随线程退役一起回收。

## 真实故障（生产环境服务不可用）

容器日志：

```
OSError: [Errno 24] Too many open files: '.../lyra/web/index.html'
```

现场：`/proc/1/fd` 里 **505 个 `/data/lyra.db` + 504 个 `lyra.db-wal`**
句柄，而进程只剩 7 个线程——连接是历史线程留下的，容器
`ulimit -n` 只有 1024，于是所有新请求都报「Too many open files」，
服务实际不可用。

根因：`StateStore` 按线程惰性建连接，并把每个连接**永久**存进
`_all`。Web 框架的 worker 线程池会随并发扩容再缩容，退役线程的
连接没人关——纯句柄泄漏。

这个测试固定住「线程退出 → 连接回收 → 连接数不随历史线程数增长」。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from lyra.db import open_store


def _touch(store) -> None:
    store.conn.execute("SELECT 1").fetchone()


def test_线程退役后连接被回收(tmp_path: Path):
    store = open_store(tmp_path / "data")
    try:
        for _round in range(5):
            threads = [
                threading.Thread(target=_touch, args=(store,)) for _ in range(30)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            time.sleep(0.05)
            store._reap_dead_threads()
        # 150 个线程跑过，连接数必须回到「当前存活线程」量级
        assert len(store._all) <= 2, (
            f"连接没有随线程回收：{len(store._all)} 个（旧实现会累积到 150）"
        )
    finally:
        store.close()


def test_回收不影响仍在使用的连接(tmp_path: Path):
    """活着的线程的连接不能被关掉。"""
    store = open_store(tmp_path / "data")
    try:
        conn = store.conn
        store._reap_dead_threads()
        assert conn.execute("SELECT 1").fetchone()[0] == 1, "活线程的连接仍可用"
    finally:
        store.close()


def test_连接在主线程上始终保留(tmp_path: Path):
    """主线程的连接是「自己那个」，回收时要留着。"""
    store = open_store(tmp_path / "data")
    try:
        store.conn.execute("SELECT 1").fetchone()
        store._reap_dead_threads()
        assert len(store._all) >= 1
        assert store.conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        store.close()
