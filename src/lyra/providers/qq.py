"""QQ 音乐适配器。

搜索与歌词接口无需登录。候选自带 interval（秒），用于时长闸门。

含 & 的整串多歌手查询会被拒绝（subcode -10002），因此查询按主歌手、
其余歌手、仅标题逐级降级。被拒绝的查询不是「没有结果」，不能据此判定未找到。

标题用字可能与本地标签不同，模块只返回候选，匹配判断留给上层。
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..config import QQProviderConfig
from ..lrc import parse_lrc
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
    "QQProvider", "QQ_SEARCH_URL", "QQ_LYRIC_URL", "QQ_SONG_DETAIL_URL",
    "qrc_to_line_lrc", "looks_like_qrc",
]

QQ_SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
QQ_LYRIC_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
#: 单曲详情。**只用于人工粘贴链接的场景**（需要把 id 显示成「这是哪一首」）。
QQ_SONG_DETAIL_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 实际：搜索要 Referer 到站点根，歌词要 Referer 到播放器页，否则可能被拒
_SEARCH_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://y.qq.com/"}
_LYRIC_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://y.qq.com/portal/player.html",
}

# QQProviderConfig 里没有熔断参数（未获授权修改 config.py），这里用保守默认值。
# 5 次连续失败即暂停 5 分钟；QQ 是主源，不希望它被轻易长时间熔断。
_QQ_BREAKER_THRESHOLD = 5
_QQ_BREAKER_INITIAL_SECONDS = 300.0

# QQ 用这个 subcode 表示「拒绝该查询」，而不是「没有匹配」
_QUERY_FORBIDDEN_SUBCODE = -10002

# QRC：行级 ``[start_ms,duration_ms]`` 前缀 + 逐字 ``(start_ms,duration_ms)``
_QRC_LINE = re.compile(r"^\[(\d{1,7}),(\d{1,7})\](.*)$")
_QRC_WORD = re.compile(r"\((\d{1,7}),(\d{1,7})\)")
_QRC_XML_CONTENT = re.compile(r'LyricContent="((?:[^"\\]|\\.)*)"', re.S)

_BASE64_CHARS = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass(frozen=True, slots=True)
class _QueryOutcome:
    """一次查询的结果。

    rejected 与「候选为空」必须分开：被拒绝说明**这次查询根本没被执行**，
    据此得出「未找到」是错的。
    """

    query: str
    candidates: tuple[Candidate, ...] = ()
    rejected: bool = False
    reject_reason: str = ""


def looks_like_qrc(text: str) -> bool:
    """判断歌词是否为 QQ 的 QRC（逐字）格式。

    两类形态都会出现：XML 包装的 ``LyricContent="..."``，以及裸的行级
    ``[start,dur]`` 前缀。LRC 的时间轴是 ``[mm:ss.xx]``（带冒号），
    与 QRC 的 ``[整数,整数]`` 不会混淆。
    """
    if not text:
        return False
    if "<LyricContent" in text or "<QrcInfos" in text:
        return True
    return any(_QRC_LINE.match(line.strip()) for line in text.splitlines())


def qrc_to_line_lrc(text: str) -> str:
    """把 QRC 降级为行级 LRC。

    为什么必须降级而不是原样保存：``[0,2544]`` 这种前缀无法被 LRC 解析器
    识别，原样放进 RawLyrics.text 会被 parse_lrc 判成 plain_text，
    synced 就是错的、歌词也就永远不会被写入。

    逐字信息被有意丢弃（仅保留每行起始时间）——降级为行级正是本函数的目的；
    ``word_level=True`` 记录了「上游其实有逐字数据」这一事实。
    """
    body = text
    if "<LyricContent" in body:
        m = _QRC_XML_CONTENT.search(body)
        if m:
            body = m.group(1)
    # 部分实现把换行写成字面量 \n
    body = body.replace("\\n", "\n").replace("&quot;", '"').replace("&amp;", "&")

    out: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _QRC_LINE.match(line)
        if not m:
            # 元信息行（[ti:] / [ar:] / [offset:]）原样保留，交给 LRC 层处理
            out.append(line)
            continue
        lyric = _QRC_WORD.sub("", m.group(3)).strip()
        if not lyric:
            continue
        out.append(f"[{_format_ms(int(m.group(1)))}]{lyric}")
    return ("\n".join(out) + "\n") if out else ""


def _format_ms(ms: int) -> str:
    minutes, rem = divmod(max(ms, 0), 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def _maybe_base64_decode(text: str) -> str:
    """QQ 在忽略 nobase64 时会返回 base64 歌词，这里做保守解码。

    保守的含义：只有「整体符合 base64 字符集」且「解码后确实出现 LRC 结构」
    才采用解码结果。否则原样返回——宁可让上层看到可疑文本，
    也不能把一段正常歌词按 base64 解成乱码。
    """
    stripped = text.strip()
    if not stripped or "[" in stripped[:200]:
        return text
    if len(stripped) % 4 != 0 or not _BASE64_CHARS.match(stripped):
        return text
    try:
        decoded = base64.b64decode(stripped, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return text
    return decoded if "[" in decoded else text


def _detail_from_song_detail(payload: object) -> dict[str, object] | None:
    """从 `fcg_play_single_song.fcg` 的响应里取出一首歌的基本信息。

    实际响应形状（2026-01 实际）：
        {"code":0,"data":[{"title":"存在 (Live)","interval":247,
                           "album":{"name":"我是歌手第二季 第2期"},
                           "singer":[{"name":"G.E.M.邓紫棋"}]}]}
    形状不认识时返回 None，**不抛异常**：这是可选的确认信息。
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    song = rows[0]
    if not isinstance(song, dict):
        return None
    interval = song.get("interval")
    album = song.get("album")
    singers = song.get("singer")
    return {
        "title": str(song.get("title") or song.get("name") or ""),
        "artists": [
            str(item.get("name"))
            for item in (singers if isinstance(singers, list) else [])
            if isinstance(item, dict) and item.get("name")
        ],
        "album": str(album.get("name") or "") if isinstance(album, dict) else "",
        "duration": float(interval) if isinstance(interval, (int, float)) else None,
    }


class QQProvider(HttpProviderBase):
    """QQ 音乐歌词来源。"""

    name = "qq"

    def __init__(
        self,
        config: QQProviderConfig,
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
                threshold=_QQ_BREAKER_THRESHOLD,
                initial_seconds=_QQ_BREAKER_INITIAL_SECONDS,
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
        # 本轮剩余的「详情 + 歌词」请求预算（QQ 搜索响应已含全部匹配所需字段，
        # 因此实际只有歌词请求会消耗它）
        self._lyric_budget = max(0, config.detail_and_lyric_limit_per_round)
        # 数字歌曲 id → songmid 的缓存（同一次操作里可能要用好几次）
        self._mid_cache: dict[str, str] = {}

    def request_headers(self) -> dict[str, str]:
        return dict(_SEARCH_HEADERS)

    # -- 查询规划 ---------------------------------------------------------

    def plan_queries(self, q: TrackQuery) -> list[str]:
        """规划有界查询序列：主歌手 → 其余歌手逐个 → 仅标题。

        为什么给「仅标题」预留名额：实际 ``G.E.M. 鄧紫棋 漂向北方`` 能命中，
        但若有 3 个歌手，逐个尝试会吃光预算，兜底策略就永远没机会跑。
        因此先为仅标题占一个名额，再用剩余预算尝试其他歌手。
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

        deadline = self._clock() + float(self._config.search_timeout_seconds)
        merged: dict[str, Candidate] = {}
        completed = 0
        rejected = 0
        truncated = False

        for query in queries:
            if self._clock() > deadline:
                truncated = True
                break
            outcome = self._search_once(query)
            if outcome.rejected:
                rejected += 1
                continue
            completed += 1
            for candidate in outcome.candidates:
                merged.setdefault(candidate.provider_id, candidate)

        candidates = list(merged.values())
        if not candidates and truncated:
            # 期限用尽且一无所获：属于 search_incomplete，不是未找到。
            # 优先于「查询被拒」判断——没跑完轮次时，「被拒」只是局部原因。
            raise BudgetExhaustedError(
                self.name,
                f"搜索期限 {self._config.search_timeout_seconds}s 用尽，"
                f"仍有查询未执行（已执行 {completed + rejected}/{len(queries)}），"
                "不能判定为未找到",
            )

        if completed == 0 and rejected > 0:
            # 全部查询都被拒绝 ≠ 没有这首歌。必须响亮失败，
            # 否则上层会把它写进 not_found 负缓存。
            raise ProviderError(
                self.name,
                f"{rejected} 个查询全部被 QQ 拒绝（subcode={_QUERY_FORBIDDEN_SUBCODE} "
                f"query forbid），本轮不能判定为未找到；查询={queries}",
            )

        limit = max(1, self._config.query_limit_per_round) * max(
            1, self._config.candidates_per_query
        )
        return candidates[:limit]

    def _search_once(self, query: str) -> _QueryOutcome:
        response = self.get(
            QQ_SEARCH_URL,
            params={
                "p": "1",
                "n": str(self._config.candidates_per_query),
                "w": query,
                "format": "json",
            },
        )
        payload = self.decode_json(response, provider=self.name)
        return self._parse_search(payload, query=query)

    def _parse_search(self, payload: object, *, query: str) -> _QueryOutcome:
        if not isinstance(payload, dict):
            raise SchemaChangedError(self.name, f"搜索响应不是 JSON 对象: {type(payload).__name__}")

        code = payload.get("code")
        if code not in (None, 0):
            # 顶层 code 非 0 含义未知，绝不当成「没有结果」
            raise ProviderError(self.name, f"搜索返回 code={code} message={payload.get('message')!r}")

        subcode = payload.get("subcode")
        if subcode not in (None, 0):
            # 实际 -10002 = query forbid：查询被拒绝，而非没有匹配
            return _QueryOutcome(
                query=query,
                rejected=True,
                reject_reason=f"subcode={subcode} message={payload.get('message')!r}",
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise SchemaChangedError(self.name, "搜索响应缺少 data 对象")
        song = data.get("song")
        if not isinstance(song, dict):
            raise SchemaChangedError(self.name, "搜索响应缺少 data.song 对象")

        raw_list = song.get("list")
        if raw_list is None:
            # 结构完好但没有 list：QQ 在 0 结果时可能省略该键
            return _QueryOutcome(query=query)
        if not isinstance(raw_list, list):
            raise SchemaChangedError(
                self.name, f"data.song.list 应为数组，实际是 {type(raw_list).__name__}"
            )

        limit = max(1, self._config.candidates_per_query)
        candidates: list[Candidate] = []
        skipped = 0
        for item in raw_list[:limit]:
            candidate = self._candidate_from_item(item)
            if candidate is None:
                skipped += 1
            else:
                candidates.append(candidate)

        if raw_list and not candidates:
            # 有数据但一条都解析不出来 → 结构已经变了，不能静默当空结果
            raise SchemaChangedError(
                self.name,
                f"data.song.list 有 {len(raw_list)} 条但全部无法解析（跳过 {skipped} 条）",
            )
        return _QueryOutcome(query=query, candidates=tuple(candidates))

    def _candidate_from_item(self, item: object) -> Candidate | None:
        """把一条搜索记录转成 Candidate；无法使用时返回 None（逐条宽松）。

        容器级结构（data/song/list）严格校验，单条记录宽松跳过：
        QQ 列表里混有各种边缘记录，若单条异常就整体报错会导致大量误报。
        但「整页都解析不出来」会被上层判为契约变化。
        """
        if not isinstance(item, dict):
            return None
        songmid = item.get("songmid")
        songname = item.get("songname")
        if not isinstance(songmid, str) or not songmid.strip():
            return None
        if not isinstance(songname, str) or not songname.strip():
            return None

        artists: tuple[str, ...] = ()
        singers = item.get("singer")
        if isinstance(singers, list):
            names = [
                s["name"].strip()
                for s in singers
                if isinstance(s, dict)
                and isinstance(s.get("name"), str)
                and s["name"].strip()
            ]
            artists = tuple(names)

        interval = item.get("interval")
        duration: float | None = None
        if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
            duration = float(interval)

        album = item.get("albumname")
        album_name = album if isinstance(album, str) else ""

        extra: dict[str, str] = {}
        if duration is not None:
            extra["interval"] = str(interval)
        for key in ("albummid", "albumname", "songid", "media_mid"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                extra[key] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                extra[key] = str(value)
        if artists:
            extra["singer"] = " / ".join(artists)

        return Candidate(
            provider=self.name,
            provider_id=songmid,
            title=songname.strip(),
            artists=artists,
            album=album_name.strip(),
            duration=duration,
            extra=extra,
        )

    # -- 歌词 -------------------------------------------------------------

    def lyrics(self, c: Candidate) -> RawLyrics | None:
        if c.provider != self.name:
            # 调用方把别的来源的候选传进来了，属于程序错误，不是来源错误
            raise ValueError(
                f"QQProvider 只能处理 provider='{self.name}' 的候选，收到 {c.provider!r}"
            )
        self._consume_lyric_budget()

        # 歌词接口只认 songmid；新版的详情页链接给的是**数字歌曲 id**，
        # 得先换成 mid 才能取词。
        songmid = self._resolve_mid(c.provider_id) or c.provider_id
        response = self.get(
            QQ_LYRIC_URL,
            params={
                "songmid": songmid,
                "format": "json",
                "nobase64": "1",
                "g_tk": "5381",
            },
            headers=_LYRIC_HEADERS,
        )
        payload = self.decode_json(response, provider=self.name)
        return self._parse_lyric(payload, provider_id=c.provider_id)

    def _resolve_mid(self, song_id: str) -> str | None:
        """把**数字歌曲 id**换成 songmid；本来就是 mid 就原样返回。

        新版的歌曲详情页是 `https://y.qq.com/n/ryqq_v2/songDetail/1839014`，
        里面给的是数字 id，而歌词接口只认 songmid。
        详情接口两种 id 都收（`songid=` / `songmid=`），所以换一次就够。
        """
        if not song_id.isdigit():
            return song_id
        cached = self._mid_cache.get(song_id)
        if cached:
            return cached
        try:
            response = self.get(
                QQ_SONG_DETAIL_URL,
                params={
                    "songid": song_id,
                    "format": "json",
                    "platform": "yqq",
                    "needNewCode": "0",
                },
            )
            payload = self.decode_json(response, provider=self.name)
        except ProviderError:
            return None
        rows = payload.get("data") if isinstance(payload, dict) else None
        mid = ""
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            mid = str(rows[0].get("mid") or "")
        if not mid:
            return None
        self._mid_cache[song_id] = mid
        return mid

    def song_detail(self, song_id: str) -> dict[str, object] | None:
        """按 mid（或数字歌曲 id）取歌曲详情（曲名/艺人/专辑/时长）。

        只服务于「人工粘贴链接」这一条路径：使用者需要看到
        「你给的这一首到底是什么」，才能确认自己没贴错。

        **取不到就返回 None，绝不抛错。** 详情只是确认用的锦上添花，
        歌词本身才是目的；为了一次详情请求失败而让整个操作失败，
        等于用小事的成功概率去赌大事。限流与结构变化在这里被有意吞掉。
        """
        try:
            if song_id.isdigit():
                response = self.get(
                    QQ_SONG_DETAIL_URL,
                    params={
                        "songid": song_id,
                        "format": "json",
                        "platform": "yqq",
                        "needNewCode": "0",
                    },
                )
            else:
                response = self.get(
                    QQ_SONG_DETAIL_URL,
                    params={
                        "songmid": song_id,
                        "format": "json",
                        "platform": "yqq",
                        "needNewCode": "0",
                    },
                )
            payload = self.decode_json(response, provider=self.name)
        except ProviderError:
            return None
        return _detail_from_song_detail(payload)

    def _consume_lyric_budget(self) -> None:
        if self._lyric_budget <= 0:
            raise BudgetExhaustedError(
                self.name,
                "本轮详情/歌词请求预算已用尽"
                f"（detail_and_lyric_limit_per_round="
                f"{self._config.detail_and_lyric_limit_per_round}）",
            )
        self._lyric_budget -= 1

    def _parse_lyric(self, payload: object, *, provider_id: str) -> RawLyrics | None:
        if not isinstance(payload, dict):
            raise SchemaChangedError(
                self.name, f"歌词响应不是 JSON 对象: {type(payload).__name__}"
            )

        retcode = payload.get("retcode")
        if retcode not in (None, 0):
            # retcode 非 0 是上游拒绝了本次请求（参数/权限/风控），
            # 与「这首歌没有歌词」是两回事，必须抛出去
            raise ProviderError(
                self.name,
                f"歌词接口 retcode={retcode} code={payload.get('code')} "
                f"subcode={payload.get('subcode')}",
            )

        raw_lyric = payload.get("lyric")
        if raw_lyric is None:
            return None
        if not isinstance(raw_lyric, str):
            raise SchemaChangedError(
                self.name, f"歌词字段应为字符串，实际是 {type(raw_lyric).__name__}"
            )

        raw_trans = payload.get("trans")
        if raw_trans is not None and not isinstance(raw_trans, str):
            raise SchemaChangedError(
                self.name, f"trans 字段应为字符串，实际是 {type(raw_trans).__name__}"
            )
        translation = raw_trans if isinstance(raw_trans, str) and raw_trans.strip() else None

        text = _maybe_base64_decode(raw_lyric)
        if not text.strip():
            # 上游明确返回空歌词：这是「该候选没有歌词」，不是错误
            return None

        word_level = False
        if looks_like_qrc(text):
            text = qrc_to_line_lrc(text)
            word_level = True
            if not text.strip():
                return None

        parsed = parse_lrc(text)
        word_level = word_level or parsed.has_word_level
        # 占位判断必须复用 parse_lrc 的结论，不能自己拿全文去匹配标记：
        # 全文里含 [ti:纯音乐示例] 这类元信息行，它们不该被当成歌词正文，
        # 直接逐行匹配会得出「不是占位」的错误结论。
        placeholder = "placeholder_only" in parsed.problems
        # 占位文案必须让 synced 为 False，否则上层的自动写入闸门会被
        # 「纯音乐，请欣赏」骗过去（明确不得写入占位词）
        synced = parsed.timestamp_count > 0 and not placeholder

        return RawLyrics(
            provider=self.name,
            provider_id=provider_id,
            text=text,
            synced=synced,
            translation=translation,
            word_level=word_level,
        )
