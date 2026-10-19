"""复核队列必须能说明「为什么没自动接受」。

## 为什么需要

实际：列表接口只返回匹配度数字，`checks` 与 `recommendation` 只在单条接口里有。
于是排队的三四十项里，每一行都只显示「0.59」这样的数字，
使用者**不点进去就完全不知道系统为什么不敢自动写入**，
而目标恰恰是「不点进去也知道为什么」。

修法：理由本来就存在 `resolutions.candidate_json`，列表加一个 JOIN 即可，
不需要对每一行再发一次请求。
"""

from __future__ import annotations

import json

from lyra.api import _staged_reason


def test_取出建议文案():
    payload = json.dumps({
        "recommendation": {"verdict": "inspect", "text": "版本证据不一致，请人工确认"},
        "checks": [
            {"label": "时长在硬闸门内", "passed": True, "detail": "差 0.00s"},
            {"label": "版本证据", "passed": False, "detail": "本地无标记 vs 候选 live"},
        ],
    })
    out = _staged_reason(payload)
    assert out["reason"] == "版本证据不一致，请人工确认"
    assert out["recommendation"]["verdict"] == "inspect"
    assert [c["label"] for c in out["failed_checks"]] == ["版本证据"]


def test_没有建议文案时用未通过的闸门兜底():
    """总比只显示一个分数强。"""
    payload = json.dumps({
        "checks": [{"label": "标题相似度", "passed": False, "detail": "0.00 < 0.85"}],
    })
    out = _staged_reason(payload)
    assert out["reason"] and "标题相似度" in out["reason"]


def test_通过的闸门不进列表():
    """队列空间有限，没过的才需要看。"""
    payload = json.dumps({
        "recommendation": {"verdict": "adopt", "text": "可以采用"},
        "checks": [{"label": "时长", "passed": True, "detail": "ok"}],
    })
    assert _staged_reason(payload)["failed_checks"] == []


def test_坏数据不让整个队列打不开():
    """一条旧记录格式不对，不该导致复核队列整体报错。"""
    for bad in (None, "", "{不是 json", "[]", 123):
        out = _staged_reason(bad)
        assert out["reason"] is None
        assert out["failed_checks"] == []
        assert out["has_evidence"] is False


def test_历史快照里的Markdown会被清理():
    """库里已有的 candidate_json 是修复前写进去的，读取层必须兜住。

    否则源码改了、界面却还在显示星号——实际就是这样。
    """
    payload = json.dumps({
        "recommendation": {
            "verdict": "inspect",
            "text": "版本证据不一致。时长相同**不能**区分版本，请人工确认。",
        },
    })
    reason = _staged_reason(payload)["reason"]
    assert "**" not in reason
    assert "不能区分版本" in reason


def test_旧闸门标签会被归一():
    """库里的快照是旧标签，读取层必须转换。

    旧标签写的是期望结果（「标题相似度达标」），未通过时界面读成
    「标题相似度达标：未通过」——自相矛盾。实际就是这样。
    """
    payload = json.dumps({
        "checks": [{"label": "标题相似度达标", "passed": False, "detail": "0.14，需 ≥0.85"}],
    })
    labels = [c["label"] for c in _staged_reason(payload)["failed_checks"]]
    assert labels == ["标题相似度"], f"旧标签未归一：{labels}"


def test_新标签保持不变():
    from lyra.api import _normalise_gate_label
    assert _normalise_gate_label("标题相似度") == "标题相似度"
    assert _normalise_gate_label("自定义闸门") == "自定义闸门"
    assert _normalise_gate_label(None) is None


def test_缺少判定记录会被显式标记():
    """没有判定记录时**必须说明**，不能显示成空白。

    空白会被理解成「没有任何问题」，而实际是「没有记录」——
    两者对复核者的含义完全相反。

    实际成因：`resolutions` 按 (provider, provider_id) 唯一，
    某个候选**没成为该来源的最终代表**时就永远不写判定记录。
    """
    assert _staged_reason(None)["has_evidence"] is False
    assert _staged_reason("{不是 json")["has_evidence"] is False
    ok = _staged_reason('{"recommendation": {"text": "x"}, "checks": []}')
    assert ok["has_evidence"] is True
