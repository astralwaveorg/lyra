"""单曲详情的解析：曲名 / 艺人 / 专辑 / 时长。

## 为什么必须有

详情只服务于「人工粘贴链接」这条路径——使用者要先看到「你给的这一首到底是
什么」，才能确认自己没贴错。它的失败模式不是崩溃，而是**安静地给出错误的
时长**：网易云给的是毫秒（320056），QQ 给的是秒（247）。差一千倍之后
时长闸门会彻底失效，而界面上不会报任何错。

另外两个来源的字段名完全不同（`ar`/`al`/`dt` vs `singer`/`album`/`interval`），
而且形状随时可能变。形状不认识时必须返回 None，**绝不能抛异常**去炸掉
整个链接流程——歌词本身才是目的。
"""

from __future__ import annotations

import pytest

from lyra.providers.netease import _detail_from_song_detail as netease_detail
from lyra.providers.qq import _detail_from_song_detail as qq_detail


def test_QQ详情():
    """实际响应形状（2026-01 对 y.qq.com 实际）。"""
    got = qq_detail({
        "code": 0,
        "data": [{
            "title": "存在 (Live)",
            "interval": 247,
            "album": {"name": "我是歌手第二季 第2期"},
            "singer": [{"name": "G.E.M.邓紫棋"}],
        }],
    })
    assert got == {
        "title": "存在 (Live)",
        "artists": ["G.E.M.邓紫棋"],
        "album": "我是歌手第二季 第2期",
        "duration": 247.0,
    }


def test_网易云详情把毫秒换算成秒():
    """320056 是毫秒。读成秒会让时长闸门差一千倍——而界面上看不出异常。"""
    got = netease_detail({
        "songs": [{
            "name": "你不是真正的快乐 (Live)",
            "dt": 320056,
            "ar": [{"name": "G.E.M.邓紫棋"}],
            "al": {"name": "我是歌手第二季 半决赛"},
        }],
    })
    assert got is not None
    assert abs(got["duration"] - 320.056) < 0.001
    assert got["title"] == "你不是真正的快乐 (Live)"
    assert got["artists"] == ["G.E.M.邓紫棋"]


def test_网易云公网旧字段名也认():
    got = netease_detail({
        "songs": [{
            "name": "x", "duration": 200.5,
            "artists": [{"name": "A"}], "album": {"name": "B"},
        }],
    })
    assert got is not None
    assert got["duration"] == 200.5, "已经是秒的值不该再除以 1000"
    assert got["artists"] == ["A"]


@pytest.mark.parametrize("payload", [None, {}, {"data": []}, {"data": [None]},
                                     "字符串", 123, {"data": "no"}])
def test_QQ形状不认识时返回_None_而不是抛错(payload):
    assert qq_detail(payload) is None


@pytest.mark.parametrize("payload", [None, {}, {"songs": []}, {"songs": [None]},
                                     "字符串", 123, {"songs": "no"}])
def test_网易云形状不认识时返回_None_而不是抛错(payload):
    assert netease_detail(payload) is None


def test_缺字段时给出可用的部分而不是整体失败():
    """少了专辑名不该让整条详情不可用——曲名与艺人已经足够确认了。"""
    got = qq_detail({"data": [{"title": "只有标题"}]})
    assert got is not None
    assert got["title"] == "只有标题"
    assert got["artists"] == []
    assert got["duration"] is None
