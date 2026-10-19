"""LRCLIB 适配器的离线测试。

为什么必须完全离线：LRCLIB 当前对来自任意网络的请求持续返回
503 ServerOverloaded（实际 NAS 与阿里云 ECS 两个网络均如此），
实际运行不可用，所以测试绝不能依赖它。除 503 响应体是真实抓取外，
其余 LRCLIB 样本均为构造（见 tests/fixtures/PROVENANCE.json）。

最重要的一条：**503 必须抛 RateLimitedError，绝不能变成「没有歌词」**。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import httpx
import pytest

from lyra.config import LrcLibProviderConfig
from lyra.providers import (
    Candidate,
    LrcLibProvider,
    ProviderError,
    RateLimitedError,
    SchemaChangedError,
    TrackQuery,
    TransientNetworkError,
    split_artists,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

CLIENT_URL = "https://lyra.example.com"
AUTH_REF = "TEST-REFERENCE-NOT-A-REAL-CREDENTIAL"


def load(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClock:
    """可注入的单调时钟，sleep 会推进时间并留痕（同 QQ 测试的理由）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def default_config(**overrides: object) -> LrcLibProviderConfig:
    base: dict[str, object] = {
        "client_name": "Lyra",
        "client_version": "1.0.0",
        "client_url": CLIENT_URL,
        "minimum_request_interval_seconds": 0.0,
        "circuit_breaker_threshold": 5,
        "circuit_breaker_initial_seconds": 1800,
        "max_backoff_seconds": 3600,
    }
    base.update(overrides)
    return LrcLibProviderConfig(**base)  # type: ignore[arg-type]


def make_router(
    *,
    get_records: dict[str, object] | None = None,
    search: object | None = None,
    get_status: int = 200,
    seen: list[str] | None = None,
    headers_seen: list[httpx.Headers] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """路由 /api/get、/api/get/{id} 与 /api/search。

    get_records 的键为 artist_name 或记录 id；未录制的请求直接失败，
    避免测试因「意外多打了一次」而假装通过。
    """
    get_records = get_records or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if headers_seen is not None:
            headers_seen.append(request.headers)
        path = request.url.path
        if path == "/api/search":
            if seen is not None:
                seen.append("search")
            if search is None:
                raise AssertionError("未录制的 /api/search 请求")
            return httpx.Response(200, json=search)
        if path.startswith("/api/get"):
            key = path[len("/api/get"):].lstrip("/") or request.url.params.get("artist_name", "")
            if seen is not None:
                seen.append(key)
            if key not in get_records:
                raise AssertionError(f"未录制的 /api/get: key={key!r}")
            return httpx.Response(get_status, json=get_records[key])
        raise AssertionError(f"未录制的请求: {request.url}")

    return handler


@pytest.fixture
def lrclib():
    """统一构造并关闭 provider，避免 httpx.Client 未关闭产生资源告警。"""
    created: list[LrcLibProvider] = []

    def factory(
        config: LrcLibProviderConfig | None = None,
        *,
        handler: Callable[[httpx.Request], httpx.Response],
        clock: FakeClock | None = None,
    ) -> LrcLibProvider:
        kwargs: dict[str, object] = {}
        if clock is not None:
            kwargs["clock"] = clock.monotonic
            kwargs["sleep"] = clock.sleep
        provider = LrcLibProvider(
            config or default_config(), transport=httpx.MockTransport(handler), **kwargs
        )
        created.append(provider)
        return provider

    yield factory
    for provider in created:
        provider.close()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_api_get命中并解析同步歌词(lrclib):
    """覆盖点：/api/get 是首选端点，字段解析错了后面全错。"""
    record = load("lrclib_get_synced")
    provider = lrclib(
        handler=make_router(
            # search 用 artist_name 命中；lyrics() 一律按 id 重新取，故两个键都要录
            get_records={"G.E.M. 鄧紫棋": record, "3396226": record}
        )
    )
    candidates = provider.search(
        TrackQuery(title="泡沫", artists=("G.E.M. 鄧紫棋",), album="Xposed", duration=258.0)
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provider == "lrclib"
    assert candidate.provider_id == "3396226"
    assert candidate.title == "泡沫"
    assert candidate.album == "Xposed"
    assert candidate.duration == 258.0
    assert candidate.extra["artistName"] == "G.E.M. 鄧紫棋"
    assert candidate.extra["instrumental"] == "false"

    lyrics = provider.lyrics(candidate)
    assert lyrics is not None
    assert lyrics.synced is True
    assert lyrics.word_level is False
    assert lyrics.translation is None
    assert "[00:00.00]测试歌词" in lyrics.text


def test_纯文本歌词返回synced为假(lrclib):
    """覆盖点：synced=False 会让上层走纯文本处理路径，
    判成 True 会写出没有时间轴的「同步歌词」。"""
    record = load("lrclib_get_plain")
    provider = lrclib(
        handler=make_router(get_records={"Some Artist": record, "4000001": record})
    )
    candidates = provider.search(TrackQuery(title="Plain Only", artists=("Some Artist",)))

    lyrics = provider.lyrics(candidates[0])

    assert lyrics is not None
    assert lyrics.synced is False
    assert "第一行" in lyrics.text


def test_纯音乐返回None且带出instrumental证据(lrclib):
    """覆盖点：纯音乐不是「没有歌词数据」，也不该被写入占位词，
    所以既要返回 None，又要让上层能拿到 instrumental 标记。"""
    record = load("lrclib_get_instrumental")
    provider = lrclib(
        handler=make_router(get_records={"Some Artist": record, "4000002": record})
    )
    candidates = provider.search(TrackQuery(title="Instrumental Piece", artists=("Some Artist",)))

    assert candidates[0].extra["instrumental"] == "true"
    assert provider.lyrics(candidates[0]) is None


def test_歌词接口404返回None而不是错误(lrclib):
    """覆盖点：记录被删除是正常的「没有歌词」，不是技术故障。"""
    provider = lrclib(handler=make_router(get_records={"9": load("lrclib_get_404")}, get_status=404))
    lyrics = provider.lyrics(Candidate(provider="lrclib", provider_id="9", title="x"))
    assert lyrics is None


def test_api_get未命中时退化到api_search(lrclib):
    """覆盖点：/api/get 是确定性匹配，查不到时仍要尝试文本搜索，
    否则大量非标准署名的曲子会被漏掉。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            seen.append("search")
            return httpx.Response(200, json=load("lrclib_search_ok"))
        seen.append("get")
        return httpx.Response(404, json=load("lrclib_get_404"))

    provider = lrclib(handler=handler)
    candidates = provider.search(TrackQuery(title="Sigur Rós Song", artists=("Earth, Wind & Fire",)))

    assert seen == ["get", "search"], "必须先 /api/get，404 后才 /api/search"
    assert [c.provider_id for c in candidates] == ["5000001", "5000002"]


# ---------------------------------------------------------------------------
# 关键：503 必须响亮失败
# ---------------------------------------------------------------------------


def test_503抛RateLimitedError而不是没有歌词(lrclib):
    """**本组测试最关键的一条。**

    实际 LRCLIB 全局过载时返回 503 ServerOverloaded 且 retry-after: 1。
    若适配器把它当成「这首歌没有歌词」，负缓存与复查计划会被彻底污染，
    而用户看到的是「已经查过、确实没有」——比查不到更糟。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json=load("lrclib_503_server_overloaded"),
            headers={"content-type": "application/json", "retry-after": "1"},
        )

    provider = lrclib(handler=handler)
    with pytest.raises(RateLimitedError) as excinfo:
        provider.search(TrackQuery(title="泡沫", artists=("G.E.M. 鄧紫棋",)))

    assert excinfo.value.retry_after_seconds == 1.0
    # 绝不能是「没找到」——异常消息里要能看出是上游过载
    assert "503" in str(excinfo.value)


def test_503后按RetryAfter自动重试一次(lrclib):
    """覆盖点：LRCLIB 过载时返回 503 + `Retry-After: 1`。

    它已经明确告诉我们等 1 秒，直接放弃并退避 60 秒纯属浪费。
    内联重试必须**真的按它说的秒数等待**，并在随后的请求上成功；
    同时必须有次数上限，不能变成对已然过载的服务端锤击。
    """
    clock = FakeClock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503, json=load("lrclib_503_server_overloaded"),
                headers={"retry-after": "1"},
            )
        return httpx.Response(200, json=load("lrclib_get_synced"))

    provider = lrclib(handler=handler, clock=clock)
    results = provider.search(TrackQuery(title="泡沫", artists=("G.E.M. 鄧紫棋",)))

    assert results, "重试后应拿到候选"
    assert calls["n"] == 2, f"应恰好重试一次，实际请求 {calls['n']} 次"
    assert clock.sleeps == [1.0], f"必须按 Retry-After 等待，实际 {clock.sleeps}"


def test_RetryAfter超过上限时不做内联重试(lrclib):
    """覆盖点：若上游说「等 3600 秒」，内联等待会把任务卡死。

    超过 `max_inline_retry_wait_seconds` 时不再内联等待，
    立刻抛出让外层退避与熔断接手。
    """
    clock = FakeClock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503, json=load("lrclib_503_server_overloaded"),
            headers={"retry-after": "3600"},
        )

    provider = lrclib(
        config=default_config(max_inline_retry_wait_seconds=5.0),
        handler=handler, clock=clock,
    )
    with pytest.raises(RateLimitedError) as excinfo:
        provider.search(TrackQuery(title="x", artists=("a",)))

    assert calls["n"] == 1, "不应该重试"
    assert clock.sleeps == [], "不应该内联等待"
    assert excinfo.value.retry_after_seconds == 3600.0, "Retry-After 仍要透出去"


def test_503的RetryAfter作为冷却下限而非全部依据(lrclib):
    """覆盖点：上游过载时只给 retry-after: 1。
    若严格照办就等于放弃退避、继续打爆上游。因此熔断冷却取
    「自身退避」与 Retry-After 的较大者。

    注意内联重试也会按 Retry-After 等待（见上一条测试），会给时间轴带来
    秒级偏移，所以这里用留有余量的时间点断言，避免把两套机制的开销混算。
    """
    clock = FakeClock()
    provider = lrclib(
        config=default_config(
            circuit_breaker_threshold=1, circuit_breaker_initial_seconds=1800,
            max_backoff_seconds=3600,
        ),
        handler=lambda request: httpx.Response(
            503, json=load("lrclib_503_server_overloaded"), headers={"retry-after": "1"}
        ),
        clock=clock,
    )
    query = TrackQuery(title="x", artists=("a",))

    with pytest.raises(RateLimitedError):
        provider.search(query)

    opened_at = clock.now
    # 冷却期内仍应被拒绝。用 1700 秒而不是贴着 1800，
    # 以避开内联重试造成的秒级偏移。
    clock.advance(1700.0)
    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert clock.now - opened_at < 1800, "冷却必须按自身退避，而不是只按 1 秒"

    clock.advance(300.0)
    # 冷却结束后进入半开，放行一个探测（仍会 503，但说明已放行）
    with pytest.raises(RateLimitedError):
        provider.search(query)


# ---------------------------------------------------------------------------
# 结构变化
# ---------------------------------------------------------------------------


def test_记录缺字段抛SchemaChangedError(lrclib):
    """覆盖点：200 却缺 trackName/artistName 说明响应结构变了，
    构造残缺 Candidate 让上层去猜会出错配。"""
    provider = lrclib(handler=make_router(get_records={"a": load("lrclib_get_malformed")}))
    with pytest.raises(SchemaChangedError):
        provider.search(TrackQuery(title="x", artists=("a",)))


def test_search返回对象而非数组抛SchemaChangedError(lrclib):
    """覆盖点：契约是数组；返回对象时必须报错，不能兜底成空列表。"""
    provider = lrclib(handler=make_router(search=load("lrclib_search_malformed")))
    # 无歌手时无法走 /api/get，只能走 /api/search——正是要测的路径
    with pytest.raises(SchemaChangedError):
        provider.search(TrackQuery(title="x"))


# ---------------------------------------------------------------------------
# 官方客户端标识
# ---------------------------------------------------------------------------


def test_必须带官方要求的两个标识头(lrclib):
    """覆盖点：LRCLIB 没有 API key，只靠这两个头识别客户端；
    漏发或两串不一致会违反其 API 使用条款。"""
    headers_seen: list[httpx.Headers] = []
    provider = lrclib(
        handler=make_router(get_records={"G.E.M. 鄧紫棋": load("lrclib_get_synced")},
                            headers_seen=headers_seen)
    )
    config = default_config()
    provider.search(TrackQuery(title="泡沫", artists=("G.E.M. 鄧紫棋",)))

    assert headers_seen, "必须真的发出请求"
    sent = headers_seen[0]
    expected = config.client_identity()
    assert sent.get("X-User-Agent") == expected
    assert sent.get("Lrclib-Client") == expected
    assert sent.get("X-User-Agent") == sent.get("Lrclib-Client")
    # 标识串形态：<AppName>/<version> (<URL>)
    assert expected.startswith("Lyra/1.0.0 (")
    assert expected.endswith(")")


def test_授权reference只在配置非空时随请求发送(lrclib):
    """覆盖点：reference 标识应用身份，按凭据对待——
    只在显式配置时发送，且绝不写进日志或异常消息。"""
    headers_seen: list[httpx.Headers] = []
    provider = lrclib(
        config=default_config(authorization_ref=AUTH_REF),
        handler=make_router(get_records={"a": load("lrclib_get_synced")}, headers_seen=headers_seen),
    )
    provider.search(TrackQuery(title="x", artists=("a",)))
    assert headers_seen[0].get("X-Lrclib-Authorization") == AUTH_REF

    # 未配置时不发送
    headers_seen.clear()
    plain = lrclib(
        config=default_config(),
        handler=make_router(get_records={"a": load("lrclib_get_synced")}, headers_seen=headers_seen),
    )
    plain.search(TrackQuery(title="x", artists=("a",)))
    assert "X-Lrclib-Authorization" not in headers_seen[0]


def test_异常消息绝不泄漏授权reference(lrclib):
    """覆盖点：reference 只出现在请求头里，异常消息只带状态码与
    响应体片段，因此不可能泄漏——这条测试把这个不变量钉死。"""
    provider = lrclib(
        config=default_config(authorization_ref=AUTH_REF),
        handler=lambda request: httpx.Response(503, json=load("lrclib_503_server_overloaded")),
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.search(TrackQuery(title="x", artists=("a",)))

    rendered = f"{excinfo.value!s}{excinfo.value!r}"
    assert AUTH_REF not in rendered


# ---------------------------------------------------------------------------
# 限流与熔断
# ---------------------------------------------------------------------------


def test_限流器按配置间隔等待(lrclib):
    """覆盖点：默认 5 秒间隔是保护这个正在过载的服务的硬约束。"""
    clock = FakeClock()
    provider = lrclib(
        config=default_config(minimum_request_interval_seconds=5.0),
        handler=make_router(get_records={"a": load("lrclib_get_synced")}),
        clock=clock,
    )
    query = TrackQuery(title="x", artists=("a",))

    provider.search(query)
    assert clock.sleeps == []

    provider.search(query)
    assert clock.sleeps == [5.0]


def test_熔断器打开后拒绝请求并可在冷却后半开(lrclib):
    """覆盖点：LRCLIB 持续 503 时，必须停止打请求而不是无限重试；
    熔断打开期间必须抛错，半开只放行一个探测。"""
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json=load("lrclib_503_server_overloaded"))

    provider = lrclib(
        config=default_config(circuit_breaker_threshold=3, circuit_breaker_initial_seconds=1800),
        handler=handler,
        clock=clock,
    )
    query = TrackQuery(title="x", artists=("a",))

    for _ in range(3):
        with pytest.raises(RateLimitedError):
            provider.search(query)
    assert len(calls) == 3

    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert len(calls) == 3, "熔断打开期间不得发出请求"

    clock.advance(1801.0)
    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert len(calls) == 4, "冷却后应放行一个半开探测"


def test_传输层超时被翻译为TransientNetworkError(lrclib):
    """覆盖点：超时是暂时性故障，必须可重试，
    不能被当成「没有歌词」。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("模拟超时")

    provider = lrclib(handler=handler)
    with pytest.raises(TransientNetworkError):
        provider.search(TrackQuery(title="x", artists=("a",)))


def test_非503的服务端错误翻译为TransientNetworkError(lrclib):
    """覆盖点：500/502/504 是可重试故障，且熔断计数要覆盖它们；
    异常基类必须提供 retry_after_seconds，否则这条路径会抛 AttributeError。"""
    provider = lrclib(handler=lambda request: httpx.Response(500, text="internal error"))
    with pytest.raises(TransientNetworkError) as excinfo:
        provider.search(TrackQuery(title="x", artists=("a",)))
    assert excinfo.value.retry_after_seconds is None


# ---------------------------------------------------------------------------
# 歌手拆分
# ---------------------------------------------------------------------------


def test_歌手拆分不切斜杠以免拆坏ACDC():
    """覆盖点：LRCLIB 只给单个 artistName 串，
    但 AC/DC 这类团名含斜杠，切了就会变成两个不存在的歌手。"""
    assert split_artists("G.E.M. 鄧紫棋") == ("G.E.M. 鄧紫棋",)
    assert split_artists("Faouzia, G.E.M. 鄧紫棋") == ("Faouzia", "G.E.M. 鄧紫棋")
    assert split_artists("Simon & Garfunkel") == ("Simon", "Garfunkel")
    assert split_artists("A feat. B") == ("A", "B")
    assert split_artists("AC/DC") == ("AC/DC",)
    assert split_artists("") == ()
