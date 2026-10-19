"""监控活动历史。

## 为什么需要

`lyra-watch` 是自动抓取与写入的**唯一执行者**，但它的活动原本只写在容器
日志里。界面看不到，使用者无法区分「它正常跑着」和「它三天前就挂了」。

心跳只保留「最后一次」，而判断是否正常靠的是**趋势**——所以还要历史。
"""

from __future__ import annotations

import time

import pytest

from lyra.db import open_store


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "d")
    yield s
    s.close()


def test_记录与读取监控活动(store):
    store.log_watch_event("已写入", "3 首", {"applied": 3, "failed": 0})
    store.log_watch_event("已启动", "正在监听")

    events = store.recent_watch_events(10)
    assert len(events) == 2
    # 最近的排在最前——后插入的「已启动」才是最新的
    assert events[0]["stage"] == "已启动"
    assert events[0]["counters"] == {}
    assert events[1]["stage"] == "已写入"
    assert events[1]["counters"] == {"applied": 3, "failed": 0}


def test_限制返回条数(store):
    for i in range(30):
        store.log_watch_event("轮次", f"第 {i} 次")
    assert len(store.recent_watch_events(limit=5)) == 5


def test_清理只删过期记录(store):
    store.log_watch_event("旧", "很久以前")
    store.conn.execute("UPDATE watch_log SET at = ?", (time.time() - 30 * 86400,))
    store.conn.commit()
    store.log_watch_event("新", "刚刚")

    removed = store.prune_watch_log(keep_days=14.0)

    assert removed == 1
    remaining = store.recent_watch_events(10)
    assert [e["stage"] for e in remaining] == ["新"]


def test_坏数据不让接口挂掉(store):
    """一条格式不对的计数不该让整个监控状态接口报错。"""
    store.conn.execute(
        "INSERT INTO watch_log(at, stage, detail, counters_json) VALUES(?,?,?,?)",
        (time.time(), "异常", "损坏的计数", "{不是 json"),
    )
    store.conn.commit()

    events = store.recent_watch_events(5)
    assert events[0]["stage"] == "异常"
    assert events[0]["counters"] == {}
