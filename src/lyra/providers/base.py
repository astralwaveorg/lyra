"""Provider 适配层基座：数据契约、异常分类、限流、熔断与 HTTP 公共逻辑。

为什么把限流与熔断放在基座而不是各适配器里：
    「不要打爆上游」和「失败必须响亮」是**所有**来源的共同约束，各写一份必然漂移。
    QQ 与 LRCLIB 的区别只在参数与端点，不在这些机制。

异常分类为什么必须分离：
    上游响应结构变化、限流、网络故障、需要登录，都可能表现为「拿不到歌词」。
    如果它们被空列表或 None 兜底，上层会把「源不可用」记成「这首歌没有歌词」，
    于是负缓存被污染、复查间隔被错误推进——这是最危险的失败模式。
    因此：**没有任何一个异常子类表示「没找到」**。
    「没找到」只能用 search() 返回空列表 / lyrics() 返回 None 表达。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import httpx

__all__ = [
    # 数据契约
    "TrackQuery",
    "Candidate",
    "RawLyrics",
    # 异常分类
    "ProviderError",
    "TransientNetworkError",
    "RateLimitedError",
    "SchemaChangedError",
    "AuthRequiredError",
    "BudgetExhaustedError",
    # 机制
    "MinIntervalLimiter",
    "CircuitState",
    "CircuitBreaker",
    "classify_http_status",
    "retry_after_seconds",
    "provider_error_for",
    "translate_transport_error",
    "HttpProviderBase",
    # 协议
    "Provider",
]


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackQuery:
    """一次刮削查询的输入：来自本地音频标签，**不做任何改写**。

    artists 已由上层按分隔符拆分。实际结论：QQ 对含 '&' 的整串歌手查询会
    直接拒绝（subcode=-10002 `query forbid`），所以多歌手必须逐个尝试。
    """

    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    """本地音频时长（秒）。这是匹配的主力判据，不是辅助信息。"""

    musicbrainz_trackid: str | None = None
    isrc: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """上游返回的一个候选录音。

    注意 extra 是 dict，因此本 dataclass 实例**不可哈希**（frozen 生成的
    __hash__ 会对 extra 求哈希而抛 TypeError）。上层若需要去重，
    请按 (provider, provider_id) 做键，不要把 Candidate 放进 set。
    """

    provider: str
    provider_id: str
    """QQ = songmid；LRCLIB = id。"""

    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    extra: Mapping[str, str] = field(default_factory=dict)
    """可追溯证据：interval / albummid / artistName / instrumental 等。"""


@dataclass(frozen=True, slots=True)
class RawLyrics:
    """上游返回的原始歌词。

    text 的形态约定：
        - 上游给的就是 LRC → 原样保存
        - 上游给的是逐字/QRC → **降级为行级 LRC 后保存**，并置 word_level=True
          （理由：QRC 的 ``[start,dur]`` 行前缀无法被 LRC 解析器识别，
          若原样放入 text，parse_lrc 会把它误判成 plain_text，synced 就会是错的）

    synced 的判定口径：text 中存在**可用的**时间轴，且内容不是占位文案。
    纯音乐占位（「纯音乐，请欣赏」）即使带时间轴也算 synced=False——
    否则上层的自动写入闸门会被占位文案骗过。
    """

    provider: str
    provider_id: str
    text: str
    synced: bool
    translation: str | None = None
    word_level: bool = False
    """上游是否提供逐字数据（KRC/QRC/增强型 LRC）。"""


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """来源层错误基类。**不表示「没找到」。**

    直接使用本基类表示「已归类但不在以下四类中」的技术失败，
    例如上游明确拒绝查询（QQ subcode=-10002）。上层必须把它当作
    技术失败处理，而**不能**据此推进「未找到」的负缓存。

    retry_after_seconds 放在基类上而不是只放在 RateLimitedError 上：
    熔断器对所有可重试失败都要读这个值，若只有限流异常才有该属性，
    处理非 503 的 5xx 时会抛 AttributeError（这个坑是变异测试发现的）。
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"[{provider}] {message}")


class TransientNetworkError(ProviderError):
    """超时、连接失败、5xx（503 除外）。可重试。"""


class RateLimitedError(ProviderError):
    """429 / 503 / 熔断打开。必须退避，**绝不可记作没有歌词**。"""


class SchemaChangedError(ProviderError):
    """响应结构不符合预期。绝不能兜底成空列表。"""


class AuthRequiredError(ProviderError):
    """需要登录或凭据失效。"""


class BudgetExhaustedError(ProviderError):
    """本轮请求预算用尽（详情/歌词请求数超限）。

    这是对给定异常分类的**扩展**：预算耗尽是「本轮没查完」，不等同于
    「没找到」或「限流」。要求此时记 search_incomplete，
    所以它必须有独立类型，不能被 RateLimitedError 或空返回值冒充。
    """


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------


class MinIntervalLimiter:
    """线程安全的最小请求间隔限流器。

    用 time.monotonic 而不是 time.time：系统校时（NTP 回拨）不该让限流器
    误判可以立刻发请求，也不该让它永久阻塞。

    睡眠在锁内进行：并发调用被串行化，从而保证任意两次请求的**起点**
    间隔不小于配置值，等价于「同一来源同时只有一个在途请求」。
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_started: float | None = None

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    def acquire(self) -> float:
        """阻塞到可以发起下一次请求，返回实际等待的秒数。"""
        with self._lock:
            now = self._clock()
            if self._last_started is None:
                self._last_started = now
                return 0.0
            wait = self._min_interval - (now - self._last_started)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._last_started = now
            return max(0.0, wait)


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """连续失败熔断器：达到阈值后打开，冷却结束进入半开，只放行一个探测。

    为什么打开期间抛 RateLimitedError 而不是返回空：
        返回空会让上层把「来源不可用」记成「这首歌没有歌词」，污染负缓存。
        必须让失败响亮地传播出去。

    Retry-After 的语义按**下限**处理：实际冷却时间取
        max(上游 Retry-After, 自身退避时长)
    因为实际 LRCLIB 在全局过载时只给 ``retry-after: 1``，若严格照办，
    等于放弃退避、继续打爆上游。
    """

    def __init__(
        self,
        *,
        threshold: int,
        initial_seconds: float,
        max_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._initial = max(0.0, float(initial_seconds))
        self._max = max(self._initial, float(max_seconds)) if max_seconds else self._initial
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._open_count = 0
        self._probe_in_flight = False

    # -- 只读观测 ---------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """返回当前状态，并顺带把到期的 OPEN 推进为 HALF_OPEN（只读视角）。"""
        with self._lock:
            self._refresh_locked()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def remaining_open_seconds(self) -> float:
        with self._lock:
            self._refresh_locked()
            if self._state is not CircuitState.OPEN:
                return 0.0
            return max(0.0, self._open_until - self._clock())

    # -- 状态机 -----------------------------------------------------------

    def _refresh_locked(self) -> None:
        """冷却到期后从 OPEN 进入 HALF_OPEN。调用方必须持锁。"""
        if self._state is CircuitState.OPEN and self._clock() >= self._open_until:
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False

    def before_request(self) -> None:
        """在发起请求前调用。熔断打开时抛 RateLimitedError。"""
        with self._lock:
            self._refresh_locked()
            if self._state is CircuitState.CLOSED:
                return
            if self._state is CircuitState.OPEN:
                remaining = max(0.0, self._open_until - self._clock())
                raise RateLimitedError(
                    "circuit",
                    f"熔断打开，剩余 {remaining:.0f} 秒（连续失败 "
                    f"{self._consecutive_failures} 次达阈值 {self._threshold}）",
                    retry_after_seconds=remaining,
                )
            # HALF_OPEN：只放行一个探测请求
            if self._probe_in_flight:
                raise RateLimitedError(
                    "circuit",
                    "半开状态已有探测请求在途，不再放行",
                    retry_after_seconds=max(0.0, self._open_until - self._clock()),
                )
            self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._open_count = 0
            self._open_until = 0.0
            self._probe_in_flight = False

    def record_failure(self, *, retry_after: float | None = None) -> None:
        with self._lock:
            self._probe_in_flight = False
            if self._state is CircuitState.HALF_OPEN:
                # 探测失败：立刻重新打开，并退避加倍
                self._consecutive_failures = self._threshold
                self._open_locked(retry_after)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._open_locked(retry_after)

    def _open_locked(self, retry_after: float | None) -> None:
        """打开熔断并计算冷却时长（调用方必须持锁）。"""
        self._open_count += 1
        # 第一次打开 = initial；之后按 2 的幂退避，上限 max
        backoff = min(self._initial * (2 ** (self._open_count - 1)), self._max)
        if retry_after is not None:
            # 取较大者：Retry-After 是等待下限，不能用来缩短自身退避
            backoff = max(backoff, retry_after)
            backoff = min(backoff, self._max)
        self._state = CircuitState.OPEN
        self._open_until = self._clock() + backoff
        self._consecutive_failures = 0


# ---------------------------------------------------------------------------
# HTTP 状态与异常映射
# ---------------------------------------------------------------------------

# 503 必须归入限流：LRCLIB 实际在全局过载时返回 **503 ServerOverloaded**
# （带 `Retry-After: 1`），而不是 429。两者都是「等一会儿可能自愈」，
# 若把 503 当作「没有歌词」，整个负缓存就废了。
_RATE_LIMITED_STATUS: frozenset[int] = frozenset({429, 503})

# 401 = 需要认证；403 = 客户端标识被服务端拦截/拒绝。
# 对 LRCLIB 而言 403 **不是**「请求太频繁」——过频是 429。LRCLIB 会过滤
# 某些默认客户端标识（例如 Jellyfin-Server），403 更可能意味着我们的
# `X-User-Agent` / `Lrclib-Client` 被拒。这类问题需要人介入（改标识），
# 因此与 401 同归，且**不计入熔断**，否则会把「标识有问题」误诊成
# 「服务不稳定」，掩盖真正的原因。
_AUTH_STATUS: frozenset[int] = frozenset({401, 403})
_NOT_FOUND_STATUS = 404

_MAX_BODY_SNIPPET = 200


def classify_http_status(status: int) -> type[ProviderError] | None:
    """把 HTTP 状态码映射到异常类型；返回 None 表示不是错误。

    404 返回 None：它表示「该记录不存在」，是正常业务结果（search 返回空、
    lyrics 返回 None），不是技术故障。
    """
    if status < 400 or status == _NOT_FOUND_STATUS:
        return None
    if status in _RATE_LIMITED_STATUS:
        return RateLimitedError
    if status in _AUTH_STATUS:
        return AuthRequiredError
    if status >= 500:
        return TransientNetworkError
    # 其余 4xx：对上游的请求模型已经不再匹配，属于契约变化
    return SchemaChangedError


def retry_after_seconds(response: httpx.Response) -> float | None:
    """解析 Retry-After（仅支持秒数形式；HTTP-date 形式忽略）。

    只读响应头，不读请求头——因此不可能把凭据带进异常消息。
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def _body_snippet(response: httpx.Response) -> str:
    """截取响应体片段用于诊断。

    只用 response.text，绝不含请求头；LRCLIB 的授权 reference 只出现在
    请求头里，因此不会经由这里泄漏到日志或异常消息。
    """
    try:
        text = response.text
    except Exception:  # pragma: no cover - 解码失败时不影响异常分类
        return "<响应体无法解码>"
    text = " ".join(text.split())
    return text[:_MAX_BODY_SNIPPET] + ("…" if len(text) > _MAX_BODY_SNIPPET else "")


def provider_error_for(response: httpx.Response, *, provider: str) -> ProviderError | None:
    """根据响应构造异常实例；返回 None 表示响应可用。"""
    cls = classify_http_status(response.status_code)
    if cls is None:
        return None
    message = f"HTTP {response.status_code}: {_body_snippet(response)}"
    if cls is RateLimitedError:
        return RateLimitedError(
            provider, message, retry_after_seconds=retry_after_seconds(response)
        )
    return cls(provider, message)


def translate_transport_error(exc: Exception, *, provider: str) -> ProviderError:
    """把 httpx 传输层异常翻译成来源层异常。"""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return TransientNetworkError(provider, f"请求超时: {type(exc).__name__}")
    if isinstance(exc, httpx.TransportError):
        return TransientNetworkError(provider, f"传输失败: {type(exc).__name__}: {exc}")
    return ProviderError(provider, f"未预期错误: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 公共 HTTP 底座
# ---------------------------------------------------------------------------


class HttpProviderBase:
    """限流 + 熔断 + 超时 + 可注入 transport 的公共底座。

    为什么用 transport 注入而不是打桩整个 HTTP 层：
        测试要验证的是「真实响应体 → 解析与分类」，用 httpx.MockTransport
        回放录制样本时，请求构造、超时、状态码分类、JSON 解析全都真实执行；
        若把 get() 整个替换掉，测试就只剩下解析逻辑，失去意义。

    熔断与限流必须成对出现：限流管「太快」，熔断管「一直失败还继续打」。
    """

    name: str = ""

    def __init__(
        self,
        *,
        limiter: MinIntervalLimiter,
        breaker: CircuitBreaker,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_inline_retries: int = 0,
        max_inline_retry_wait: float = 0.0,
    ) -> None:
        self._limiter = limiter
        self._breaker = breaker
        self._timeout = float(timeout_seconds)
        self._clock = clock
        self._sleep = sleep
        # 有界内联重试：只用于「上游明确告诉我们等几秒」的情况。
        # 实际 LRCLIB 过载时返回 503 + `Retry-After: 1`；它已经说了等 1 秒，
        # 直接放弃并退避 60 秒纯属浪费。但也不能无限重试，所以同时限制
        # 次数与单次等待时长，超过任一上限就交给外层退避与熔断。
        self._max_inline_retries = max(0, int(max_inline_retries))
        self._max_inline_retry_wait = max(0.0, float(max_inline_retry_wait))
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            # 所有网络调用都必须有超时，且只能是配置里的值
            self._client = httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
            )
            self._owns_client = True

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpProviderBase:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 供子类覆写 -------------------------------------------------------

    def request_headers(self) -> dict[str, str]:
        """每个请求都会带上的头。子类覆写。"""
        return {}

    # -- 请求 -------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """发起一次受管 GET。熔断打开时抛 RateLimitedError，不发出请求。

        对「上游明确给出 Retry-After 且等待很短」的过载/限流做**有界**内联
        重试：实际 LRCLIB 过载时返回 503 + `Retry-After: 1`，等 1 秒再试的
        收益远大于直接退避 60 秒；但次数与单次等待都有硬上限，避免对已经
        过载的服务端变成锤击。
        """
        merged_headers = {**self.request_headers(), **(headers or {})}
        attempts_left = self._max_inline_retries

        while True:
            # 顺序很重要：熔断检查在限流之前，被拒绝时不应白白睡一个间隔
            self._breaker.before_request()
            self._limiter.acquire()
            try:
                response = self._client.get(
                    url, params=dict(params or {}), headers=merged_headers
                )
            except Exception as exc:  # noqa: BLE001 - 统一翻译传输层异常
                error = translate_transport_error(exc, provider=self.name)
                if isinstance(error, TransientNetworkError):
                    self._breaker.record_failure()
                raise error from exc

            error = provider_error_for(response, provider=self.name)
            if error is None:
                self._breaker.record_success()
                return response

            # 只有「可能自愈」的失败才计入熔断；结构变化/鉴权问题需要人介入，
            # 计入熔断只会掩盖真正的原因。
            if isinstance(error, (TransientNetworkError, RateLimitedError)):
                self._breaker.record_failure(retry_after=error.retry_after_seconds)

            wait = (
                error.retry_after_seconds
                if isinstance(error, RateLimitedError)
                else None
            )
            if (
                attempts_left > 0
                and wait is not None
                and wait <= self._max_inline_retry_wait
            ):
                attempts_left -= 1
                self._sleep(wait)
                continue

            raise error

    @staticmethod
    def decode_json(response: httpx.Response, *, provider: str) -> object:
        """解析 JSON；失败即契约变化，绝不兜底成空结构。"""
        try:
            return response.json()
        except ValueError as exc:
            raise SchemaChangedError(
                provider,
                f"响应不是合法 JSON（HTTP {response.status_code}）: "
                f"{_body_snippet(response)}",
            ) from exc


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


class Provider(Protocol):
    """来源适配接口。上层匹配层只依赖这个协议。"""

    name: str

    def search(self, q: TrackQuery) -> list[Candidate]:
        """按查询返回候选。

        返回值语义：
            ``[]``  = 上游确实执行了查询且没有匹配 → **只有这种情况**才能进
                      not_found 负缓存
            非空    = 候选列表（顺序即来源返回顺序，未做匹配打分）

        异常语义（调用方必须区分，任一异常都**没有**得出「未找到」的结论）：
            RateLimitedError      限流/过载/熔断 → 退避重试
            TransientNetworkError 超时/连接失败 → 退避重试
            SchemaChangedError    上游结构变化 → 需人工修适配器
            ProviderError         其余技术失败（如上游明确拒绝查询）
            BudgetExhaustedError  本轮没查完（search_incomplete）→ 重新排队，
                                  绝不可记 not_found
        """
        ...

    def lyrics(self, c: Candidate) -> RawLyrics | None:
        """取候选的歌词。

        ``None`` 表示上游明确表示该候选没有可用歌词（含纯音乐占位），
        属于正常业务结果；抛异常才表示本轮没查成，
        **同样绝不可记成「没有歌词」**。
        """
        ...
