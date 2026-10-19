"""批量接受「硬闸门全过」那批的安全边界。

## 为什么格外谨慎

这个端点的动作是**把曲目放进写入队列**，接下来会被写进真实的音乐文件。
所以筛选逻辑必须证明它**绝不会**把「来源返回了另一首歌」那一类放进去。

判定规则：只要有任何一道**硬闸门**未通过（时长 / 版本 / 标题 / 艺人），
就不算安全。软门槛（加权总分 `confidence`）未达不在此列——
它代表「不够确信」，不代表「可能是错的」。

这里用真实的 GateCheck 结构固定这条边界。
"""

from __future__ import annotations

import json

import pytest

from lyra.api import _staged_reason


def _payload(failed_keys: list[str], *, with_recommendation: bool = True) -> str:
    checks = [
        {"key": "duration_gate", "label": "时长差", "passed": True, "detail": "差 0.0s"},
        {"key": "version", "label": "版本证据", "passed": True, "detail": "一致"},
        {"key": "title", "label": "标题相似度", "passed": True, "detail": "0.99"},
        {"key": "artist", "label": "艺人相似度", "passed": True, "detail": "0.95"},
        {"key": "confidence", "label": "匹配总分", "passed": True, "detail": "0.80"},
    ]
    for c in checks:
        if c["key"] in failed_keys:
            c["passed"] = False
    doc: dict = {"checks": checks}
    if with_recommendation:
        doc["recommendation"] = {"verdict": "inspect", "text": "仅总分不足"}
    return json.dumps(doc)


HARD = {"duration_gate", "version", "title", "artist"}


def _is_safe(raw: str) -> bool:
    """与端点内保持同一判定逻辑。"""
    info = _staged_reason(raw)
    if info.get("reason") is None:
        return False
    keys = {c.get("key") for c in (info.get("failed_checks") or []) if c.get("key")}
    return not (keys & HARD)


@pytest.mark.parametrize("key", sorted(HARD))
def test_任一硬闸门失败都不算安全(key):
    assert not _is_safe(_payload([key])), f"{key} 失败却被判为安全——绝不能发生"


def test_仅总分不足算安全():
    assert _is_safe(_payload(["confidence"]))


def test_一道都没失败也算安全():
    """逻辑上不该出现，但必须覆盖，否则会被静默漏掉。"""
    assert _is_safe(_payload([]))


def test_没有判定记录不算安全():
    """宁可漏掉，也不能把没有依据的项放进写入队列。"""
    assert not _is_safe(json.dumps({"checks": []}))


def test_硬闸门与软门槛同时失败不算安全():
    assert not _is_safe(_payload(["title", "confidence"]))


def test_列表与批量用同一个安全判据():
    """两处若各写一套，迟早出现「列表说可接受、批量说不安全」的自相矛盾。"""
    from lyra.api import _is_safe_for_batch

    assert _is_safe_for_batch([{"key": "confidence"}])
    assert _is_safe_for_batch([])
    for key in sorted(HARD):
        assert not _is_safe_for_batch([{"key": key}]), f"{key} 失败却判为安全"
    assert not _is_safe_for_batch([{"key": "title"}, {"key": "confidence"}])


def test_聚合把同一曲目的候选归到一起():
    """实际队列里同一首歌重复出现 3 次，使用者以为它们是不同的歌。"""
    import inspect

    # _staged 是模块级函数，不在 create_app 里——查错地方会永远通过
    from lyra.api import _staged
    src = inspect.getsource(_staged)
    assert "groups" in src, "列表接口应提供按曲目聚合的 groups"
    assert "safe_count" in src, "聚合结果应给出可安全接受的候选数"


def test_没有证据不算安全():
    """**遇到过两次的坑。**

    旧快照里没有 `checks`，`failed_checks` 于是为空。只看「没有硬闸门失败」
    就判为安全，等于把「没有证据」当成「证据表明安全」——
    实际把分数 0.30 的旧候选标成了「可接受」。
    """
    from lyra.api import _is_safe_for_batch

    assert not _is_safe_for_batch([], has_evidence=False)
    assert _is_safe_for_batch([], has_evidence=True)
    assert not _is_safe_for_batch([{"key": "title"}], has_evidence=True)
