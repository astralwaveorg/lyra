"""QQ 适配器的离线测试。

为什么必须完全离线：QQ 接口可用但**只在特定网络可达**（NAS 可，ECS 返回 500），
CI 与开发机不能假设可达；而且真实抓取会被上游限流。因此全部用
httpx.MockTransport 回放 tests/fixtures 下的录制样本——
请求构造、超时、状态码分类、JSON 解析仍然真实执行，
只有网络字节被替换，避免打桩过深导致测试失去意义。

样本来源与可信度见 tests/fixtures/PROVENANCE.json。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import httpx
import pytest

from lyra.config import QQProviderConfig
from lyra.lrc import parse_lrc
from lyra.providers import (
    BudgetExhaustedError,
    Candidate,
    ProviderError,
    QQProvider,
    RateLimitedError,
    SchemaChangedError,
    TrackQuery,
    TransientNetworkError,
    looks_like_qrc,
    qrc_to_line_lrc,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# 实际：265 秒那条的 songmid
MID_PIAOXIANGBEIFANG_LIVE = "0034DzuD2REIYW"
MID_PAOMO = "001X0PDf0W4lBq"


def load(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClock:
    """可注入的单调时钟，sleep 会推进时间并留痕。

    真实 time.monotonic 无法在测试里快进，用假时钟才能断言
    「确实等了配置的间隔」，而不是「碰巧跑得慢」。
    """

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


def make_router(
    *,
    searches: dict[str, object] | None = None,
    lyrics: dict[str, object] | None = None,
    seen: list[str] | None = None,
    before: Callable[[], None] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """按查询串/ songmid 路由到录制样本。

    未录制的请求直接失败而不是返回空——否则测试会因为「意外多打了一次」
    而假装通过，掩盖查询规划的错误。
    """
    searches = searches or {}
    lyrics = lyrics or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if before is not None:
            before()
        path = request.url.path
        if "client_search_cp" in path:
            keyword = request.url.params.get("w")
            if seen is not None:
                seen.append(keyword or "")
            if keyword not in searches:
                raise AssertionError(f"未录制的搜索查询: {keyword!r}")
            return httpx.Response(200, json=searches[keyword])
        if "fcg_query_lyric_new" in path:
            songmid = request.url.params.get("songmid")
            if seen is not None:
                seen.append(songmid or "")
            if songmid not in lyrics:
                raise AssertionError(f"未录制的歌词请求 songmid={songmid!r}")
            return httpx.Response(200, json=lyrics[songmid])
        raise AssertionError(f"未录制的请求: {request.url}")

    return handler


@pytest.fixture
def qq():
    """统一构造并关闭 provider，避免 httpx.Client 未关闭产生资源告警。"""
    created: list[QQProvider] = []

    def factory(
        config: QQProviderConfig | None = None,
        *,
        handler: Callable[[httpx.Request], httpx.Response],
        clock: FakeClock | None = None,
    ) -> QQProvider:
        # 测试默认把间隔设为 0；需要验证限流的用例自己传 2.0
        cfg = config or QQProviderConfig(minimum_request_interval_seconds=0)
        kwargs: dict[str, object] = {}
        if clock is not None:
            kwargs["clock"] = clock.monotonic
            kwargs["sleep"] = clock.sleep
        provider = QQProvider(cfg, transport=httpx.MockTransport(handler), **kwargs)
        created.append(provider)
        return provider

    yield factory
    for provider in created:
        provider.close()


# ---------------------------------------------------------------------------
# 多歌手查询（本次实际的核心问题）
# ---------------------------------------------------------------------------


def test_多歌手含与号的查询被拒也不会判定为未找到(qq):
    """覆盖点：实际 ARTIST='黄明志 & G.E.M. 鄧紫棋' 时，
    含 '&' 的整串查询被 QQ 以 subcode=-10002 拒绝（并非「没有匹配」）。
    若适配器把这种 0 结果当成未找到，这首歌就会被永久写进 not_found 负缓存。
    """
    seen: list[str] = []
    provider = qq(
        handler=make_router(
            searches={
                "黄明志 漂向北方": load("qq_search_artist_huangmingzhi_piaoxiangbeifang"),
                "G.E.M. 鄧紫棋 漂向北方": load("qq_search_artist_gem_piaoxiangbeifang"),
                "漂向北方": load("qq_search_title_only_piaoxiangbeifang"),
            },
            seen=seen,
        )
    )
    query = TrackQuery(title="漂向北方", artists=("黄明志", "G.E.M. 鄧紫棋"), duration=265.0)

    candidates = provider.search(query)

    hit = [c for c in candidates if c.provider_id == MID_PIAOXIANGBEIFANG_LIVE]
    assert hit, "必须靠单歌手查询命中 265 秒的那一条"
    assert hit[0].title == "飘向北方 (Live)"
    assert hit[0].duration == 265.0
    assert hit[0].artists == ("G.E.M.邓紫棋",)

    # 三条查询都按规划执行了：被拒的那条不能中断整个轮次
    assert seen == [
        "黄明志 漂向北方",
        "G.E.M. 鄧紫棋 漂向北方",
        "漂向北方",
    ]


def test_查询规划_主歌手优先并为仅标题保留名额(qq):
    """覆盖点：仅标题查询是兜底策略，若有 3 个歌手就吃光预算，
    兜底就永远不会执行（实际正是单歌手/仅标题救回了 飘向北方）。"""
    provider = qq(handler=make_router(searches={}), config=QQProviderConfig(query_limit_per_round=3))
    query = TrackQuery(title="漂向北方", artists=("黄明志", "G.E.M. 鄧紫棋"))
    assert provider.plan_queries(query) == [
        "黄明志 漂向北方",
        "G.E.M. 鄧紫棋 漂向北方",
        "漂向北方",
    ]

    # 预算 1：只跑主歌手
    tight = qq(handler=make_router(searches={}), config=QQProviderConfig(query_limit_per_round=1))
    assert tight.plan_queries(query) == ["黄明志 漂向北方"]

    # 4 个歌手、预算 3：主歌手 + 1 个其他 + 仅标题（仍为兜底留位）
    many = TrackQuery(title="T", artists=("A", "B", "C", "D"))
    assert provider.plan_queries(many) == ["A T", "B T", "T"]

    # 无歌手时只有标题
    assert provider.plan_queries(TrackQuery(title="T")) == ["T"]
    # 无标题时退化为按歌手名查询
    assert provider.plan_queries(TrackQuery(title="", artists=("A", "B"))) == ["A", "B"]


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


def test_搜索字段解析正确(qq):
    """覆盖点：songmid/songname/singer/interval/albumname 是匹配层的全部输入，
    字段名或类型解析错了，时长闸门就会失效。"""
    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=make_router(
            searches={"G.E.M. 鄧紫棋 漂向北方": load("qq_search_artist_gem_piaoxiangbeifang")}
        ),
    )

    candidates = provider.search(TrackQuery(title="漂向北方", artists=("G.E.M. 鄧紫棋",)))

    assert len(candidates) == 20
    target = next(c for c in candidates if c.provider_id == MID_PIAOXIANGBEIFANG_LIVE)
    assert target.provider == "qq"
    assert target.title == "飘向北方 (Live)"
    assert target.artists == ("G.E.M.邓紫棋",)
    assert target.duration == 265.0
    assert isinstance(target.duration, float)
    # interval 原样留作证据，供复核页面展示
    assert target.extra["interval"] == "265"
    assert target.album == ""
    # 去重：20 条候选的 provider_id 互不相同
    assert len({c.provider_id for c in candidates}) == 20


def test_空结果与查询被拒是两种情况(qq):
    """覆盖点：『上游查询成功但没有匹配』才是 not_found；
    『上游拒绝查询』是技术失败。两者混为一谈会污染负缓存。"""
    empty_ok = {"code": 0, "subcode": 0, "data": {"song": {"curnum": 0, "list": [], "totalnum": 0}}}
    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=make_router(searches={"a x": empty_ok}),
    )
    # 正常空结果 → 空列表，不抛异常
    assert provider.search(TrackQuery(title="x", artists=("a",))) == []


def test_全部查询被拒绝时抛错而不是返回空列表(qq):
    """覆盖点：实际 subcode=-10002 时 list 为空但 code=0，
    只看列表长度就会得出「未找到」的错误结论。"""
    rejected = load("qq_search_artist_huangmingzhi_piaoxiangbeifang")
    provider = qq(
        handler=make_router(
            searches={
                "黄明志 漂向北方": rejected,
                "G.E.M. 鄧紫棋 漂向北方": rejected,
                "漂向北方": rejected,
            }
        )
    )
    query = TrackQuery(title="漂向北方", artists=("黄明志", "G.E.M. 鄧紫棋"))

    with pytest.raises(ProviderError) as excinfo:
        provider.search(query)

    # 必须是「一般技术失败」，不能是限流/网络/结构这类会误导上层的具体分类
    assert not isinstance(
        excinfo.value, (RateLimitedError, TransientNetworkError, SchemaChangedError)
    )
    assert "拒绝" in str(excinfo.value)


@pytest.mark.parametrize(
    "fixture",
    ["qq_search_missing_data", "qq_search_missing_song", "qq_search_unparseable_items"],
)
def test_搜索结构异常抛SchemaChangedError(qq, fixture):
    """覆盖点：结构变化绝不能被空列表兜底后算作 not_found（设计约束）。"""
    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=make_router(searches={"a x": load(fixture)}),
    )
    with pytest.raises(SchemaChangedError):
        provider.search(TrackQuery(title="x", artists=("a",)))


def test_搜索遇503抛RateLimitedError而不是空结果(qq):
    """覆盖点：503 表示上游过载，必须退避重试，
    若被当成「没有歌词」，负缓存与复查计划就全错了。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"message": "busy", "name": "ServerOverloaded", "statusCode": 503},
            headers={"retry-after": "1"},
        )

    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=handler,
    )
    with pytest.raises(RateLimitedError) as excinfo:
        provider.search(TrackQuery(title="x", artists=("a",)))
    # Retry-After 必须被解析出来，熔断据此决定冷却（按上限处理）
    assert excinfo.value.retry_after_seconds == 1.0


def test_非503的服务端错误翻译为TransientNetworkError(qq):
    """覆盖点：500/502/504 是可重试的服务端故障，不是限流。
    这条用例是变异测试逼出来的——此前只有 503 的用例，
    导致「非限流的 5xx 会因缺少 retry_after 属性而抛 AttributeError」
    这个真实缺陷没有被覆盖。"""
    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=lambda request: httpx.Response(502, text="<html>bad gateway</html>"),
    )
    with pytest.raises(TransientNetworkError) as excinfo:
        provider.search(TrackQuery(title="x", artists=("a",)))
    # 熔断器要能读到该属性（基类提供，默认为 None）
    assert excinfo.value.retry_after_seconds is None


def test_搜索期限用尽且无结果时抛BudgetExhausted而不是未找到(qq):
    """覆盖点：预算/期限用尽属于 search_incomplete，
    报告成「未找到」会让用户以为这首歌真的没有歌词。"""
    clock = FakeClock()
    empty_ok = {"code": 0, "subcode": 0, "data": {"song": {"list": []}}}

    # 每次请求把时钟推进 5 秒，使轮次期限（1 秒）很快超限
    provider = qq(
        config=QQProviderConfig(
            minimum_request_interval_seconds=0,
            query_limit_per_round=3,
            search_timeout_seconds=1.0,
        ),
        handler=make_router(searches={"A T": empty_ok, "B T": empty_ok, "T": empty_ok},
                            before=lambda: clock.advance(5)),
        clock=clock,
    )
    with pytest.raises(BudgetExhaustedError):
        provider.search(TrackQuery(title="T", artists=("A", "B")))


# ---------------------------------------------------------------------------
# 歌词
# ---------------------------------------------------------------------------


def test_歌词返回带时间轴LRC(qq):
    """覆盖点：synced 是自动写入闸门的输入，判错会写入纯文本或拒绝好歌词。"""
    provider = qq(handler=make_router(lyrics={MID_PAOMO: load("qq_lyric_paomo")}))

    lyrics = provider.lyrics(Candidate(provider="qq", provider_id=MID_PAOMO, title="泡沫"))

    assert lyrics is not None
    assert lyrics.synced is True
    assert lyrics.word_level is False
    assert lyrics.translation is None
    assert "[00:00.00]测试歌词" in lyrics.text
    # 未被 base64 误处理：正文与录制样本逐字一致
    assert lyrics.text == load("qq_lyric_paomo")["lyric"]
    assert parse_lrc(lyrics.text).timestamp_count > 0


def test_数字歌曲id会换成songmid再取词(qq):
    """覆盖点：新版详情页给的是数字 id，而歌词接口只认 songmid。

    实际：使用者贴 `https://y.qq.com/n/ryqq_v2/songDetail/1839014`
    时报「没有找到歌曲 id」；换成能解析之后，取词又必须用换出来的 mid。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "fcg_play_single_song" in path:
            assert request.url.params.get("songid") == "1839014"
            return httpx.Response(200, json={
                "code": 0,
                "data": [{"mid": MID_PAOMO, "title": "泡沫", "interval": 240,
                          "singer": [{"name": "A"}], "album": {"name": "B"}}],
            })
        if "fcg_query_lyric_new" in path:
            songmid = request.url.params.get("songmid")
            seen.append(songmid or "")
            return httpx.Response(200, json=lyrics_payload)
        raise AssertionError(f"未录制的请求: {request.url}")

    lyrics_payload = load("qq_lyric_paomo")
    provider = qq(handler=handler)

    result = provider.lyrics(Candidate(provider="qq", provider_id="1839014", title="泡沫"))

    assert result is not None
    assert result.synced is True
    # 取词用的是换出来的 songmid，不是数字 id
    assert seen == [MID_PAOMO]
    # 再取一次不再重复请求详情（缓存）
    provider.lyrics(Candidate(provider="qq", provider_id="1839014", title="泡沫"))
    assert seen == [MID_PAOMO, MID_PAOMO]


def test_数字歌曲id取详情也认(qq):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "fcg_play_single_song" in request.url.path
        assert request.url.params.get("songid") == "1839014"
        return httpx.Response(200, json={
            "code": 0,
            "data": [{"mid": MID_PAOMO, "title": "泡沫", "interval": 240,
                      "singer": [{"name": "A"}], "album": {"name": "B"}}],
        })

    provider = qq(handler=handler)

    detail = provider.song_detail("1839014")
    assert detail is not None
    assert detail["title"] == "泡沫"


def test_歌词为空时返回None且不抛未找到语义(qq):
    """覆盖点：上游空歌词是正常结果，不是错误，更不是「未找到」。"""
    provider = qq(handler=make_router(lyrics={"empty": load("qq_lyric_empty")}))
    assert provider.lyrics(Candidate(provider="qq", provider_id="empty", title="x")) is None


def test_占位文案不算可用同步歌词(qq):
    """覆盖点：『纯音乐，请欣赏』带时间轴，若 synced=True 就会被自动写入，
    而规范明令禁止写入占位词。原文仍要保留，供上层记录 suspected_no_lyrics。"""
    provider = qq(handler=make_router(lyrics={"ph": load("qq_lyric_placeholder")}))

    lyrics = provider.lyrics(Candidate(provider="qq", provider_id="ph", title="x"))

    assert lyrics is not None
    assert lyrics.synced is False
    assert "纯音乐" in lyrics.text


def test_歌词retcode非零抛错而不是当成没有歌词(qq):
    """覆盖点：retcode 非 0 是上游拒绝本次请求（参数/风控），
    与「这首歌没有歌词」完全不同，静默返回 None 会污染负缓存。"""
    provider = qq(handler=make_router(lyrics={"bad": load("qq_lyric_retcode_error")}))
    with pytest.raises(ProviderError) as excinfo:
        provider.lyrics(Candidate(provider="qq", provider_id="bad", title="x"))
    assert not isinstance(excinfo.value, (RateLimitedError, TransientNetworkError))


def test_歌词请求预算用尽后抛BudgetExhausted而不是返回None(qq):
    """覆盖点：预算耗尽是「本轮没查完」，返回 None 会被记成「没有歌词」。"""
    provider = qq(
        config=QQProviderConfig(
            minimum_request_interval_seconds=0, detail_and_lyric_limit_per_round=1
        ),
        handler=make_router(lyrics={"m": load("qq_lyric_paomo")}),
    )
    candidate = Candidate(provider="qq", provider_id="m", title="x")
    assert provider.lyrics(candidate) is not None
    with pytest.raises(BudgetExhaustedError):
        provider.lyrics(candidate)


def test_传入其他来源的候选属于调用方错误(qq):
    """覆盖点：跨来源误用是程序缺陷，必须立刻炸掉而不是发出错误请求。"""
    provider = qq(handler=make_router())
    with pytest.raises(ValueError):
        provider.lyrics(Candidate(provider="lrclib", provider_id="1", title="x"))


# ---------------------------------------------------------------------------
# 逐字 / QRC 与 base64
# ---------------------------------------------------------------------------


def test_QRC降级为行级LRC并标记逐字(qq):
    """覆盖点：QRC 的 [start,dur] 前缀无法被 LRC 解析器识别，
    原样保存会被判成 plain_text，歌词就永远不会被写入。"""
    provider = qq(handler=make_router(lyrics={"qrc": load("qq_lyric_qrc")}))

    lyrics = provider.lyrics(Candidate(provider="qq", provider_id="qrc", title="测试逐字"))

    assert lyrics is not None
    assert lyrics.word_level is True
    assert lyrics.synced is True
    assert "[ti:测试逐字]" in lyrics.text          # 元信息保留
    assert "[00:00.00]词：测试" in lyrics.text
    assert "[00:02.54]第一行歌词" in lyrics.text
    assert "(2544,600)" not in lyrics.text          # 逐字标签已剥离
    # 降级结果必须能被项目自己的解析器认可
    assert parse_lrc(lyrics.text).timestamp_count == 3


def test_looks_like_qrc不误判标准LRC():
    """覆盖点：QRC 与 LRC 的时间轴语法不同，误判会把好歌词转坏。"""
    assert looks_like_qrc(load("qq_lyric_qrc")["lyric"]) is True
    assert looks_like_qrc(load("qq_lyric_paomo")["lyric"]) is False
    assert looks_like_qrc("") is False
    assert looks_like_qrc("[00:12.34]普通歌词") is False


def test_qrc解析保留无文本的元信息行():
    """QRC 里 [ti:]/[ar:] 等行没有 [start,dur] 前缀，必须原样带出。"""
    converted = qrc_to_line_lrc("[ti:标题]\n[ar:歌手]\n[1000,500]正文(1000,500)\n")
    assert converted == "[ti:标题]\n[ar:歌手]\n[00:01.00]正文\n"


def test_base64歌词自动解码(qq):
    """覆盖点：QQ 忽略 nobase64 参数时会返回 base64，
    不解码就会把一整段 base64 当成歌词写进文件。"""
    provider = qq(handler=make_router(lyrics={"b64": load("qq_lyric_base64")}))

    lyrics = provider.lyrics(Candidate(provider="qq", provider_id="b64", title="x"))

    assert lyrics is not None
    assert lyrics.synced is True
    assert "第一行歌词" in lyrics.text


# ---------------------------------------------------------------------------
# 限流与熔断
# ---------------------------------------------------------------------------


def test_限流器按配置间隔等待(qq):
    """覆盖点：间隔是保护上游的硬约束，必须真的等待，
    而且要用单调时钟——系统校时不能让间隔失效。"""
    clock = FakeClock()
    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=2.0, query_limit_per_round=1),
        handler=make_router(
            searches={
                "G.E.M. 鄧紫棋 漂向北方": load("qq_search_artist_gem_piaoxiangbeifang")
            }
        ),
        clock=clock,
    )
    query = TrackQuery(title="漂向北方", artists=("G.E.M. 鄧紫棋",))

    provider.search(query)
    assert clock.sleeps == [], "首次请求不应等待"

    provider.search(query)
    assert clock.sleeps == [2.0], "第二次请求必须等待完整间隔"

    provider.search(query)
    assert clock.sleeps == [2.0, 2.0]


def test_熔断器连续失败后打开_冷却后半开只放行一个探测(qq):
    """覆盖点：熔断打开期间必须拒绝发请求且抛错（不能静默返回空），
    半开时只放行一个探测，避免上游刚恢复就被打垮。"""
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"message": "busy", "statusCode": 503})

    provider = qq(
        config=QQProviderConfig(minimum_request_interval_seconds=0, query_limit_per_round=1),
        handler=handler,
        clock=clock,
    )
    query = TrackQuery(title="x", artists=("a",))

    for _ in range(5):
        with pytest.raises(RateLimitedError):
            provider.search(query)
    assert len(calls) == 5, "阈值 5 次失败后应打开熔断"

    # 打开期间：不发请求，且必须抛错
    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert len(calls) == 5, "熔断打开期间不得发出任何请求"

    # 冷却结束 → 半开，放行恰好一个探测
    clock.advance(301.0)
    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert len(calls) == 6, "半开只放行一个探测请求"

    # 探测失败 → 立刻重新打开，后续再次拒绝
    with pytest.raises(RateLimitedError):
        provider.search(query)
    assert len(calls) == 6, "探测失败后应重新打开，不再放行"
