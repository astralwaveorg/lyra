"""网易云音乐歌词来源。

搜索与歌词接口无需登录，歌词时间轴为毫秒精度。它与 QQ 的曲库覆盖不重合，
作为补充来源。

请求必须带 Referer 和浏览器 UA：缺少 Referer 时接口返回空结果而不是错误，
会被上层误判为「没有歌词」并污染负缓存。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from ..config import NetEaseProviderConfig
from ..lrc import parse_lrc
from ..models import LyricsState
from .base import (
    BudgetExhaustedError,
    Candidate,
    CircuitBreaker,
    HttpProviderBase,
    MinIntervalLimiter,
    ProviderError,
    RawLyrics,
    SchemaChangedError,
    TrackQuery,
)

__all__ = [
    "NETEASE_SEARCH_URL",
    "NETEASE_LYRIC_URL",
    "NETEASE_DETAIL_URL",
    "NetEaseProvider",
]

NETEASE_SEARCH_URL = "https://music.163.com/api/search/get/web"
NETEASE_LYRIC_URL = "https://music.163.com/api/song/lyric"
#: 单曲详情，**只用于人工粘贴链接时的确认**。
NETEASE_DETAIL_URL = "https://music.163.com/api/song/detail"

_BREAKER_THRESHOLD = 5
_BREAKER_INITIAL_SECONDS = 60.0

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    # 关键：见模块文档，缺 Referer 会得到「空结果」而非报错
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _detail_from_song_detail(payload: object) -> dict[str, object] | None:
    """从网易云单曲详情里取出基本信息。

    实际响应形状（自建服务，2026-01 实际）：
        {"songs":[{"name":"你不是真正的快乐 (Live)","dt":320056,
                   "ar":[{"name":"G.E.M.邓紫棋"}],"al":{"name":"我是歌手第二季 半决赛"}}]}
    公网旧接口用 `artists`/`album`/`duration` 命名，两种都认。
    形状不认识时返回 None，不抛异常。
    """
    if not isinstance(payload, dict):
        return None
    songs = payload.get("songs")
    if not isinstance(songs, list) or not songs:
        return None
    song = songs[0]
    if not isinstance(song, dict):
        return None
    artists_raw = song.get("ar") or song.get("artists") or []
    album_raw = song.get("al") or song.get("album")
    duration = song.get("dt") or song.get("duration")
    if isinstance(duration, (int, float)) and duration > 10000:
        # 毫秒 → 秒。320056ms 与 320.056s 差一千倍，读错会让时长闸门彻底失效。
        duration = float(duration) / 1000.0
    return {
        "title": str(song.get("name") or ""),
        "artists": [
            str(item.get("name"))
            for item in (artists_raw if isinstance(artists_raw, list) else [])
            if isinstance(item, dict) and item.get("name")
        ],
        "album": str(album_raw.get("name") or "") if isinstance(album_raw, dict) else "",
        "duration": float(duration) if isinstance(duration, (int, float)) else None,
    }


class NetEaseProvider(HttpProviderBase):
    """网易云音乐歌词来源。"""

    name = "netease"

    def __init__(
        self,
        config: NetEaseProviderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        effective_clock = clock or time.monotonic
        effective_sleep = sleep or time.sleep
        super().__init__(
            limiter=MinIntervalLimiter(
                config.minimum_request_interval_seconds,
                clock=effective_clock,
                sleep=effective_sleep,
            ),
            breaker=CircuitBreaker(
                threshold=_BREAKER_THRESHOLD,
                initial_seconds=_BREAKER_INITIAL_SECONDS,
                clock=effective_clock,
            ),
            timeout_seconds=config.request_timeout_seconds,
            transport=transport,
            client=client,
            clock=effective_clock,
            sleep=effective_sleep,
            max_inline_retries=config.max_inline_retries,
            max_inline_retry_wait=config.max_inline_retry_wait_seconds,
        )
        self._lyric_budget = max(0, config.detail_and_lyric_limit_per_round)

    def request_headers(self) -> dict[str, str]:
        return dict(_HEADERS)

    # -- 端点适配 ---------------------------------------------------------
    # 自建 NeteaseCloudMusicApi（binaryify/netease_cloud_music_api）的
    # **路径与参数名**与公网旧接口不同，但**响应结构完全一致**
    # （`{"result":{"songs":[...]}}` 与 `{"lrc":{"lyric":...}}`），
    # 所以只需在这里换算地址，解析逻辑一行都不用改。
    #
    # 实际自建服务对旧路径返回 404（不兼容），必须走新路径。

    @property
    def _self_hosted(self) -> bool:
        return bool((self._config.base_url or "").strip())

    def _search_target(self, query: str) -> tuple[str, dict[str, object]]:
        limit = str(self._config.candidates_per_query)
        if self._self_hosted:
            base = self._config.base_url.strip().rstrip("/")
            return f"{base}/search", {"keywords": query, "limit": limit}
        return NETEASE_SEARCH_URL, {
            "s": query, "type": 1, "offset": 0, "limit": limit,
        }

    def _detail_target(self, song_id: str) -> tuple[str, dict[str, object]]:
        if self._self_hosted:
            base = self._config.base_url.strip().rstrip("/")
            # 自建版实际接受逗号分隔的 ids（`/song/detail?ids=28341146`）
            return f"{base}/song/detail", {"ids": song_id}
        return NETEASE_DETAIL_URL, {"ids": f"[{song_id}]", "limit": "1", "offset": "0"}

    def _lyric_target(self, provider_id: str) -> tuple[str, dict[str, object]]:
        if self._self_hosted:
            base = self._config.base_url.strip().rstrip("/")
            return f"{base}/lyric", {"id": provider_id}
        return NETEASE_LYRIC_URL, {
            "id": provider_id, "lv": "-1", "kv": "-1", "tv": "-1",
        }

    # -- 查询规划 ---------------------------------------------------------

    def plan_queries(self, q: TrackQuery) -> list[str]:
        """有界查询序列：主歌手 → 其余歌手逐个 → 仅标题。

        与 QQ 同一策略：为「仅标题」预留名额。实际多歌手串（含 ``&``）
        会让搜索返回空，而仅标题往往仍能命中，所以兜底必须有位置。
        """
        title = " ".join(q.title.split())
        artists = [a.strip() for a in q.artists if a and a.strip()]
        budget = max(1, int(self._config.query_limit_per_round))
        planned: list[str] = []

        def add(text: str) -> None:
            text = " ".join(text.split())
            if text and text not in planned:
                planned.append(text)

        if title:
            if artists:
                add(f"{artists[0]} {title}")
            rest = budget - len(planned) - 1
            for artist in artists[1:]:
                if rest <= 0:
                    break
                add(f"{artist} {title}")
                rest -= 1
            add(title)
        else:
            for artist in artists:
                if len(planned) >= budget:
                    break
                add(artist)
        return planned[:budget]

    # -- 搜索 -------------------------------------------------------------

    def search(self, q: TrackQuery) -> list[Candidate]:
        queries = self.plan_queries(q)
        self._lyric_budget = max(0, self._config.detail_and_lyric_limit_per_round)
        if not queries:
            return []

        merged: dict[str, Candidate] = {}
        completed = 0
        last_error: ProviderError | None = None

        for query in queries:
            try:
                payload = self._search_once(query)
            except ProviderError as exc:
                # 记录但继续尝试下一个查询串：
                # 单个查询串失败（如被拒）不等于「这首歌没有歌词」
                last_error = exc
                continue
            completed += 1
            for cand in self._parse_search(payload):
                merged.setdefault(cand.provider_id, cand)

        if completed == 0 and last_error is not None:
            # 所有查询串都失败 → 这是来源故障，必须响亮地抛出，
            # 绝不能让上层记成「未找到」
            raise last_error
        return list(merged.values())

    def _search_once(self, query: str) -> object:
        # 不在此处包 try/except：HttpProviderBase.get() 已经把传输层异常与
        # HTTP 状态码统一翻译成 ProviderError 子类，重复包裹只会产生死代码。
        url, params = self._search_target(query)
        response = self.get(
            url,
            params=params,
        )
        return self.decode_json(response, provider=self.name)

    def _parse_search(self, payload: object) -> list[Candidate]:
        if not isinstance(payload, dict):
            raise SchemaChangedError(self.name, "搜索响应不是 JSON 对象")

        code = payload.get("code")
        # 网易云在无结果时返回 {"result": {}, "code": 200} 或 {"code": 405}，
        # 两者都表示「没找到」——不是错误，用空列表表达
        if code is not None and int(code) not in (200,) and int(code) in (405, 400):
            return []
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        songs = result.get("songs")
        if songs is None:
            return []
        if not isinstance(songs, list):
            raise SchemaChangedError(self.name, "result.songs 不是数组")

        out: list[Candidate] = []
        for item in songs:
            cand = self._candidate_from_item(item)
            if cand is not None:
                out.append(cand)
        return out

    def _candidate_from_item(self, item: object) -> Candidate | None:
        if not isinstance(item, dict):
            return None
        song_id = item.get("id")
        if song_id is None:
            return None
        title = str(item.get("name") or "").strip()
        if not title:
            return None

        artists: list[str] = []
        raw_artists = item.get("artists") or item.get("ar")
        if isinstance(raw_artists, list):
            for entry in raw_artists:
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "").strip()
                    if name:
                        artists.append(name)

        album = ""
        album_id = ""
        raw_album = item.get("album") or item.get("al")
        if isinstance(raw_album, dict):
            album = str(raw_album.get("name") or "")
            album_id = str(raw_album.get("id") or "")

        duration: float | None = None
        for key in ("duration", "dt"):
            ms = item.get(key)
            if isinstance(ms, (int, float)) and ms > 0:
                duration = float(ms) / 1000.0
                break

        extra: dict[str, str] = {}
        if album_id:
            extra["album_id"] = album_id

        return Candidate(
            provider=self.name,
            provider_id=str(song_id),
            title=title,
            artists=tuple(artists),
            album=album,
            duration=duration,
            extra=extra,
        )

    # -- 歌词 -------------------------------------------------------------

    def lyrics(self, c: Candidate) -> RawLyrics | None:
        if c.provider != self.name:
            raise ValueError(
                f"NetEaseProvider 只能处理 provider='{self.name}' 的候选，收到 {c.provider!r}"
            )
        self._consume_lyric_budget()
        url, params = self._lyric_target(c.provider_id)
        response = self.get(url, params=params)
        payload = self.decode_json(response, provider=self.name)
        return self._parse_lyric(payload, provider_id=c.provider_id)

    def song_detail(self, song_id: str) -> dict[str, object] | None:
        """按 id 取歌曲详情。取不到返回 None，**不抛错**（理由同 QQProvider）。"""
        url, params = self._detail_target(song_id)
        try:
            response = self.get(url, params=params)
            payload = self.decode_json(response, provider=self.name)
        except ProviderError:
            return None
        return _detail_from_song_detail(payload)

    def _consume_lyric_budget(self) -> None:
        if self._lyric_budget <= 0:
            raise BudgetExhaustedError(
                self.name,
                "本轮歌词请求预算已用尽"
                f"（detail_and_lyric_limit_per_round="
                f"{self._config.detail_and_lyric_limit_per_round}）",
            )
        self._lyric_budget -= 1

    def _parse_lyric(self, payload: object, *, provider_id: str) -> RawLyrics | None:
        if not isinstance(payload, dict):
            raise SchemaChangedError(self.name, "歌词响应不是 JSON 对象")

        # 两种**明确**的「这首歌没有歌词」标记。
        # 注意这与「来源故障」是两回事：它们是上游给出的确定答案，返回 None；
        # 而请求失败、限流、结构变化一律抛异常。
        if payload.get("nolyric") is True or payload.get("uncollected") is True:
            return None

        raw_lrc = payload.get("lrc")
        text = ""
        if isinstance(raw_lrc, dict):
            text = str(raw_lrc.get("lyric") or "")
        if not text.strip():
            # 上游明确返回空歌词 → 没找到，用 None 表达
            return None

        translation: str | None = None
        raw_translation = payload.get("tlyric")
        if isinstance(raw_translation, dict):
            candidate_translation = str(raw_translation.get("lyric") or "").strip()
            if candidate_translation:
                translation = candidate_translation

        # klyric 是逐字（卡拉OK）数据。我们仍然写出行级 LRC，
        # 只把「上游有逐字数据」这个事实记下来，供上层判断质量。
        word_level = False
        raw_k = payload.get("klyric")
        if isinstance(raw_k, dict) and str(raw_k.get("lyric") or "").strip():
            word_level = True

        parsed = parse_lrc(text)
        # synced 的口径与其它来源一致：必须真的有可用时间轴。
        # 占位文案（「纯音乐，请欣赏」）即使带时间轴也会被判为 SUSPICIOUS，
        # 因此这里只认 SYNCED，避免占位文案骗过自动写入闸门。
        synced = parsed.state is LyricsState.SYNCED
        return RawLyrics(
            provider=self.name,
            provider_id=provider_id,
            text=text,
            synced=synced,
            translation=translation,
            word_level=word_level,
        )
