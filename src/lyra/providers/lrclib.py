"""LRCLIB 适配器。

LRCLIB 没有 API key，只要求客户端标识头。authorization_ref 是可公开验证的
注册记录，不是密钥，但仍按凭据处理，不写入日志或异常。

服务过载时返回 503 ServerOverloaded + Retry-After。503 必须映射为
RateLimitedError，不能当作没有歌词；退避和熔断由 base 处理。

优先使用 /api/get 确定性查询，仅在 404 时退化到 /api/search。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx

from ..config import LrcLibProviderConfig
from .base import (
    Candidate,
    CircuitBreaker,
    HttpProviderBase,
    MinIntervalLimiter,
    RawLyrics,
    SchemaChangedError,
    TrackQuery,
)

__all__ = ["LrcLibProvider", "LRCLIB_BASE_URL", "split_artists"]

LRCLIB_BASE_URL = "https://lrclib.net"

# 歌手串切分。**有意不切 '/'**：AC/DC 会被切成两个歌手，这是真实存在的团名。
# feat./ft./with 必须带两侧空白：`\bfeat\.?\b` 这种写法在 "feat." 上会因
# 句点后没有单词边界而只吃掉 "feat"，把句点留进下一个歌手名（实际遇到过）。
_ARTIST_SEPARATOR = re.compile(
    r"\s*(?:,|&|;)\s*|\s+(?:feat\.?|ft\.?|with)\s+",
    re.IGNORECASE,
)

# /api/get 的尝试上限。每多一次尝试就多一次请求，
# 而服务正在过载——两次足够覆盖「主歌手」与「完整合唱署名」两种写法。
_MAX_GET_ATTEMPTS = 2


def split_artists(name: str) -> tuple[str, ...]:
    """把 LRCLIB 的单串 artistName 拆成多歌手。

    为什么要拆：LRCLIB 只给一个字符串（如 ``"Faouzia, G.E.M. 鄧紫棋"``），
    而上层的 TrackQuery.artists 是拆好的元组；不拆就无法做集合交集匹配。
    原始串会原样保留在 Candidate.extra["artistName"] 里，供上层回退比对。
    """
    parts = [p.strip() for p in _ARTIST_SEPARATOR.split(name)]
    cleaned = tuple(p for p in parts if p)
    if cleaned:
        return cleaned
    stripped = name.strip()
    return (stripped,) if stripped else ()


class LrcLibProvider(HttpProviderBase):
    """LRCLIB 歌词来源。

    有意不做「用 search 响应缓存歌词」的优化：search 响应是否包含
    plainLyrics/syncedLyrics 未能在本环境实际（服务全程 503）。
    若它其实省略了歌词字段，缓存就会让 lyrics() 返回 None，
    从而把「没查」伪装成「没有歌词」——这正是本项目最不能接受的失败模式。
    因此 lyrics() 一律走 /api/get/{id}。
    """

    name = "lrclib"

    def __init__(
        self,
        config: LrcLibProviderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        # 必须用 config 的工厂方法，不要自己拼标识串（官方约定易变）
        self._identity = config.client_identity()
        effective_clock = clock or time.monotonic
        effective_sleep = sleep or time.sleep
        super().__init__(
            limiter=MinIntervalLimiter(
                config.minimum_request_interval_seconds,
                clock=effective_clock,
                sleep=effective_sleep,
            ),
            breaker=CircuitBreaker(
                threshold=config.circuit_breaker_threshold,
                initial_seconds=config.circuit_breaker_initial_seconds,
                max_seconds=config.max_backoff_seconds,
                clock=effective_clock,
            ),
            timeout_seconds=config.request_timeout_seconds,
            transport=transport,
            client=client,
            clock=effective_clock,
            sleep=effective_sleep,
            # 过载时有界内联重试：LRCLIB 会给出 Retry-After，直接照做比
            # 自行退避 60 秒有效得多；上限由配置控制，防止锤击。
            max_inline_retries=config.max_inline_retries,
            max_inline_retry_wait=config.max_inline_retry_wait_seconds,
        )

    def request_headers(self) -> dict[str, str]:
        # 两个头必须是同一个串；这是 LRCLIB 识别客户端的方式
        identity = self._identity
        headers = {
            "X-User-Agent": identity,
            "Lrclib-Client": identity,
            "Accept": "application/json",
        }
        if self._config.authorization_ref:
            # 本地约定，**不是** LRCLIB 官方要求的头：把已登记的授权 reference
            # 随请求发送，便于上游溯源。仅出现在请求头里，
            # 因此不会经异常消息或日志泄漏（异常只带 HTTP 状态与响应体片段）。
            headers["X-Lrclib-Authorization"] = self._config.authorization_ref
        return headers

    # -- 搜索 -------------------------------------------------------------

    def search(self, q: TrackQuery) -> list[Candidate]:
        title = q.title.strip()
        artists = [a.strip() for a in q.artists if a and a.strip()]

        if title and artists:
            for artist_name in self._get_attempt_artists(artists):
                response = self.get(
                    f"{LRCLIB_BASE_URL}/api/get",
                    params=self._get_params(artist_name=artist_name, q=q, title=title),
                )
                if response.status_code == 200:
                    record = self.decode_json(response, provider=self.name)
                    return [self._record_to_candidate(record)]
                # 404 → 该署名组合没有记录，换下一个尝试或退化到 search
            return self._search_by_text(q, artists=artists, title=title)

        return self._search_by_text(q, artists=artists, title=title)

    def _get_attempt_artists(self, artists: list[str]) -> list[str]:
        """构造 /api/get 的 artist_name 尝试序列（有界）。"""
        attempts = [artists[0]]
        if len(artists) > 1:
            # 第二种写法：完整合唱署名。Picard 常把多歌手写成 "A & B"
            attempts.append(" & ".join(artists))
        return attempts[:_MAX_GET_ATTEMPTS]

    def _get_params(self, *, artist_name: str, q: TrackQuery, title: str) -> dict[str, str]:
        params = {"artist_name": artist_name, "track_name": title}
        album = q.album.strip()
        if album:
            params["album_name"] = album
        if q.duration is not None and q.duration > 0:
            # duration 是 LRCLIB 侧的筛选条件，能显著降低同名异版本误配
            params["duration"] = str(int(round(q.duration)))
        return params

    def _search_by_text(self, q: TrackQuery, *, artists: list[str], title: str) -> list[Candidate]:
        query_text = " ".join(part for part in [*artists[:1], title] if part).strip()
        if not query_text:
            return []
        response = self.get(f"{LRCLIB_BASE_URL}/api/search", params={"q": query_text})
        payload = self.decode_json(response, provider=self.name)
        if not isinstance(payload, list):
            raise SchemaChangedError(
                self.name,
                f"/api/search 应返回数组，实际是 {type(payload).__name__}",
            )
        return [self._record_to_candidate(item) for item in payload]

    # -- 记录解析 ---------------------------------------------------------

    def _record_to_candidate(self, record: object) -> Candidate:
        """严格校验必要字段；缺字段即契约变化。

        这里必须严格：``/api/get`` 200 却没有 trackName/artistName，
        说明响应结构已经变了，不能构造一个残缺 Candidate 让上层去猜。
        """
        if not isinstance(record, dict):
            raise SchemaChangedError(
                self.name, f"记录应为 JSON 对象，实际是 {type(record).__name__}"
            )

        record_id = record.get("id")
        if isinstance(record_id, bool) or not isinstance(record_id, (int, str)):
            raise SchemaChangedError(self.name, f"记录缺少可用的 id: {record_id!r}")
        provider_id = str(record_id).strip()
        if not provider_id:
            raise SchemaChangedError(self.name, "记录 id 为空")

        title = record.get("trackName")
        if not isinstance(title, str) or not title.strip():
            raise SchemaChangedError(self.name, f"记录缺少 trackName: {title!r}")

        artist_name = record.get("artistName")
        if not isinstance(artist_name, str) or not artist_name.strip():
            raise SchemaChangedError(self.name, f"记录缺少 artistName: {artist_name!r}")

        album = record.get("albumName")
        if album is not None and not isinstance(album, str):
            raise SchemaChangedError(
                self.name, f"albumName 应为字符串或 null，实际是 {type(album).__name__}"
            )

        duration_raw = record.get("duration")
        duration: float | None = None
        if duration_raw is not None:
            if isinstance(duration_raw, bool) or not isinstance(duration_raw, (int, float)):
                raise SchemaChangedError(
                    self.name, f"duration 应为数字或 null，实际是 {type(duration_raw).__name__}"
                )
            duration = float(duration_raw)

        extra: dict[str, str] = {"artistName": artist_name}
        if isinstance(album, str) and album.strip():
            extra["albumName"] = album
        instrumental = record.get("instrumental")
        if isinstance(instrumental, bool):
            # 纯音乐标记是「无歌词」的重要证据，必须带出来给上层
            extra["instrumental"] = "true" if instrumental else "false"
        if duration is not None:
            extra["duration"] = str(duration)

        return Candidate(
            provider=self.name,
            provider_id=provider_id,
            title=title.strip(),
            artists=split_artists(artist_name),
            album=album.strip() if isinstance(album, str) else "",
            duration=duration,
            extra=extra,
        )

    # -- 歌词 -------------------------------------------------------------

    def lyrics(self, c: Candidate) -> RawLyrics | None:
        if c.provider != self.name:
            raise ValueError(
                f"LrcLibProvider 只能处理 provider='{self.name}' 的候选，收到 {c.provider!r}"
            )
        response = self.get(f"{LRCLIB_BASE_URL}/api/get/{c.provider_id}")
        if response.status_code == 404:
            # 记录已不存在：正常的「没有歌词」，不是错误
            return None
        record = self.decode_json(response, provider=self.name)
        return self._lyrics_from_record(record, provider_id=c.provider_id)

    def _lyrics_from_record(self, record: object, *, provider_id: str) -> RawLyrics | None:
        if not isinstance(record, dict):
            raise SchemaChangedError(
                self.name, f"记录应为 JSON 对象，实际是 {type(record).__name__}"
            )

        synced = record.get("syncedLyrics")
        plain = record.get("plainLyrics")
        for key, value in (("syncedLyrics", synced), ("plainLyrics", plain)):
            if value is not None and not isinstance(value, str):
                raise SchemaChangedError(
                    self.name, f"{key} 应为字符串或 null，实际是 {type(value).__name__}"
                )

        # 优先同步歌词：它是更完整的形式，上层要做降级也更容易
        if isinstance(synced, str) and synced.strip():
            return RawLyrics(
                provider=self.name,
                provider_id=provider_id,
                text=synced,
                synced=True,
                translation=None,
                word_level=False,
            )
        if isinstance(plain, str) and plain.strip():
            return RawLyrics(
                provider=self.name,
                provider_id=provider_id,
                text=plain,
                synced=False,
                translation=None,
                word_level=False,
            )
        # 纯音乐或确实无歌词 → None；instrumental 标记在 Candidate.extra 里，
        # 由上层区分「纯音乐」与「没有歌词」
        return None
