"""来源烟雾探测的回归测试（2.0.2：探测查询曾误传 dict 导致 AttributeError）。"""

from __future__ import annotations

from dataclasses import dataclass

from conftest import make_flac

from lyra.cli import probe_providers
from lyra.db import open_store
from lyra.providers.base import ProviderBlockedError, ProviderError, TrackQuery


@dataclass
class _Recorded:
    """记录 search 收到的查询对象，供断言其类型。"""

    name: str
    exc: Exception | None = None
    query: TrackQuery | None = None


class _FakeProvider:
    def __init__(self, name: str, *, exc: Exception | None = None, recorder: list) -> None:
        self._name = name
        self._exc = exc
        self._recorder = recorder

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: TrackQuery):
        self._recorder.append(_Recorded(self._name, exc=self._exc, query=query))
        if self._exc is not None:
            raise self._exc
        return []


def test_探测查询是TrackQuery而非字典(tmp_path):
    """回归：探测曾把普通 dict 传给 search()，适配器访问 q.title 即崩，
    健康面板因此显示 AttributeError 而非真实的来源状态。"""
    store = open_store(tmp_path / "data")
    recorded: list = []
    ok_provider = _FakeProvider("ok_src", recorder=recorded)
    probe_providers(store, [ok_provider])

    assert len(recorded) == 1
    sent = recorded[0].query
    assert isinstance(sent, TrackQuery), "探测必须构造 TrackQuery 数据类"
    assert sent.title == "Lyra"
    # 健康表：ok 计 1，且成功探测清空 last_error
    summary = store.provider_health_summary()[0]
    assert summary["provider"] == "ok_src"
    assert summary["ok"] == 1
    assert summary["schema_error"] == 0


def test_探测失败按类型分类(tmp_path):
    """风控记 blocked；其它来源异常记 schema_error；两者都带可读原因。"""
    store = open_store(tmp_path / "data")
    blocked = _FakeProvider("qq", exc=ProviderBlockedError("qq", "返回 HTML 风控页"), recorder=[])
    broken = _FakeProvider("netease", exc=ProviderError("netease", "需要登录"), recorder=[])
    probe_providers(store, [blocked, broken])

    by_provider = {row["provider"]: row for row in store.provider_health_summary()}
    assert by_provider["qq"]["blocked"] == 1
    assert by_provider["qq"]["last_error"] and "HTML" in by_provider["qq"]["last_error"]
    assert by_provider["netease"]["schema_error"] == 1


def test_成功探测清除旧错误(tmp_path):
    """部署修复后当天重跑探测：ok 计入且 last_error 被清空，
    健康面板不再显示昨天的 AttributeError。"""
    store = open_store(tmp_path / "data")
    store.bump_provider_health("qq", schema_error=True, error="AttributeError: 旧错误")
    ok_provider = _FakeProvider("qq", recorder=[])
    probe_providers(store, [ok_provider])

    summary = store.provider_health_summary()[0]
    assert summary["ok"] == 1
    assert summary["last_error"] is None, "成功探测应清空 last_error"


def _flac(tmp_path):
    return make_flac(tmp_path / "x.flac")
