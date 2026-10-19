"""进度上报的测试。

## 为什么需要

实际遇到过：进度按**条数**节流（`considered % 10 == 0`），而每首约 20 秒
（三个来源各带限流间隔）。于是界面十分钟里一直显示「已处理 30 首」——
**看起来和卡死完全一样**，我自己都被骗去查了两轮「是否卡住」。

进度信息的全部意义就是让人知道「还在动」。所以固定三件事：
1. 节流按**时间**，不按条数；
2. 报出**当前正在处理哪一首**（使用者认歌名，不认序号）；
3. 报出**已用时与预计剩余**。
"""

from __future__ import annotations

from lyra.pipeline import (
    _PROGRESS_INTERVAL_SECONDS,
    _describe_target,
    _human_seconds,
)


def test_节流间隔短于人的耐心阈值():
    """十分钟不变的文字会被判断成死锁。间隔必须是秒级。"""
    assert _PROGRESS_INTERVAL_SECONDS <= 5.0, (
        f"进度间隔 {_PROGRESS_INTERVAL_SECONDS}s 太长，界面会看起来像卡死"
    )


def test_时长格式化():
    assert _human_seconds(45) == "45 秒"
    assert _human_seconds(200) == "3 分 20 秒"
    assert _human_seconds(4000) == "1 小时 6 分"
    assert _human_seconds(-1) == "—"


def test_用曲名描述当前曲目而不是序号():
    class Ctx:
        title = "回忆的沙漏"
        artists = ("G.E.M. 鄧紫棋",)

    class T:
        context = Ctx()
        rel_path = "a/b/c.flac"

    desc = _describe_target(T())
    assert "回忆的沙漏" in desc
    assert "G.E.M. 鄧紫棋" in desc


def test_没有标题时退回文件名():
    class Ctx:
        title = ""
        artists = ()

    class T:
        context = Ctx()
        rel_path = "专辑/未知曲目.flac"

    assert _describe_target(T()) == "未知曲目.flac"
