"""Provider 适配层：把「从某个来源找歌词」抽象成可替换的 Provider。

为什么要有这一层：
    QQ 与 LRCLIB 的端点、字段、限流特性完全不同，但上层的匹配逻辑
    （时长闸门、版本判定、置信度、负缓存）与来源无关。把它隔开之后，
    新增一个来源不需要改匹配层，匹配层的改进也不用碰适配器。

为什么「未声明的来源」必须报错：
    要求不得偷偷回退到其他来源并把结果标成 QQ 的。
    工厂只认显式列出的名字，拼错或未实现的名字直接抛错，
    避免「以为在用 QQ，实际用了别的源」这种静默行为。

异常分类（见 base.py）：没有任何一个异常子类表示「没找到」。
    没找到只能用 search() 返回空列表 / lyrics() 返回 None 表达。
"""

from __future__ import annotations

from ..config import ProvidersConfig
from .base import (
    AuthRequiredError,
    BudgetExhaustedError,
    Candidate,
    CircuitBreaker,
    CircuitState,
    HttpProviderBase,
    MinIntervalLimiter,
    Provider,
    ProviderBlockedError,
    ProviderError,
    RateLimitedError,
    RawLyrics,
    SchemaChangedError,
    TrackQuery,
    TransientNetworkError,
    classify_http_status,
    provider_error_for,
    retry_after_seconds,
    translate_transport_error,
)
from .lrclib import LRCLIB_BASE_URL, LrcLibProvider, split_artists
from .netease import NETEASE_LYRIC_URL, NETEASE_SEARCH_URL, NetEaseProvider
from .qq import (
    QQ_LYRIC_URL,
    QQ_SEARCH_URL,
    QQProvider,
    looks_like_qrc,
    qrc_to_line_lrc,
)

__all__ = [
    # 契约
    "TrackQuery",
    "Candidate",
    "RawLyrics",
    "Provider",
    # 异常
    "ProviderError",
    "ProviderBlockedError",
    "TransientNetworkError",
    "RateLimitedError",
    "SchemaChangedError",
    "AuthRequiredError",
    "BudgetExhaustedError",
    # 机制
    "MinIntervalLimiter",
    "CircuitBreaker",
    "CircuitState",
    "HttpProviderBase",
    "classify_http_status",
    "provider_error_for",
    "retry_after_seconds",
    "translate_transport_error",
    # 适配器
    "QQProvider",
    "LrcLibProvider",
    "NetEaseProvider",
    "QQ_SEARCH_URL",
    "QQ_LYRIC_URL",
    "LRCLIB_BASE_URL",
    "NETEASE_SEARCH_URL",
    "NETEASE_LYRIC_URL",
    "qrc_to_line_lrc",
    "looks_like_qrc",
    "split_artists",
    # 工厂
    "SUPPORTED_PROVIDERS",
    "create_provider",
    "create_providers",
]

SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"qq", "lrclib", "netease"})


def create_provider(
    name: str,
    config: ProvidersConfig,
    **kwargs: object,
) -> QQProvider | LrcLibProvider | NetEaseProvider:
    """按名字构造单个来源。未知名字直接报错（绝不静默换源）。

    kwargs 透传给适配器（transport / client / clock / sleep），
    用于测试注入 MockTransport 与假时钟。
    """
    if name == "qq":
        return QQProvider(
            config.qq, user_agent=config.user_agent or None, **kwargs
        )  # type: ignore[arg-type]
    if name == "lrclib":
        return LrcLibProvider(config.lrclib, **kwargs)  # type: ignore[arg-type]
    if name == "netease":
        return NetEaseProvider(
            config.netease, user_agent=config.user_agent or None, **kwargs
        )  # type: ignore[arg-type]
    raise ValueError(
        f"未知来源 {name!r}；已实现: {', '.join(sorted(SUPPORTED_PROVIDERS))}。"
        "不允许使用未在配置中声明的来源。"
    )


def create_providers(config: ProvidersConfig, **kwargs: object) -> list[Provider]:
    """按 config.enabled 的顺序构造全部已启用来源。"""
    return [create_provider(name, config, **kwargs) for name in config.enabled]
