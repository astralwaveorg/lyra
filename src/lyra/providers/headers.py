"""直连上游的请求头集中管理。

为什么单独成模块：QQ / 网易云都是非官方公开接口，请求头的伪装质量直接影响
可用性（缺 Referer 时网易云返回空结果而非报错，会被上层误判成「没有歌词」
并污染负缓存——这是踩过的真实坑）。把 UA 与伪装头集中到这里：

1. `providers.user_agent` 配置项可以整体替换 UA（默认用内置的 Chrome 伪装值）；
2. 各适配器只声明自己的差异项（QQ 的歌词接口要 Referer 到播放器页）；
3. 风控特征升级时只改一处。
"""

from __future__ import annotations

__all__ = ["DEFAULT_USER_AGENT", "browser_headers"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def browser_headers(
    *,
    user_agent: str | None = None,
    referer: str | None = None,
    accept: str | None = None,
    accept_language: str | None = None,
) -> dict[str, str]:
    """构造一组浏览器伪装头。空段不输出，避免空值头反而成为特征。"""
    headers: dict[str, str] = {
        "User-Agent": (user_agent or "").strip() or DEFAULT_USER_AGENT,
    }
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers
