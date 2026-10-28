"""LRC 解析、分类与规范化。

这是准确性的第一道关：区分「有时间轴」与「纯文本」，并识别占位文案。
注意 的告诫——格式检查通过不等于歌词对应本录音。
本模块只做格式与结构判定，不做内容正确性判断。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace

from .models import LyricsState, ParsedLyrics

__all__ = [
    "decode_lyrics_bytes",
    "parse_lrc",
    "parse_lrc_lines",
    "normalize_lrc",
    "merge_bilingual",
    "format_ms",
    "is_placeholder_only",
    "content_hash",
    "LrcLine",
    "PLACEHOLDER_MARKERS",
]

# --------------------------------------------------------------------------
# 正则
# --------------------------------------------------------------------------

# [mm:ss.xx] / [mm:ss.xxx] / [mm:ss:xx] / [m:ss]
# 第 3 组是小数分隔符（'.' 或 ':'），必须捕获：':' 表示毫秒直读，'.' 表示小数
_TIME_TAG = re.compile(r"\[(\d{1,4}):([0-5]?\d)(?:([.:])(\d{1,3}))?\]")

# <mm:ss.xx> 逐字（增强型 LRC）。同样要捕获小数点分隔符。
_WORD_TAG = re.compile(r"<(\d{1,4}):([0-5]?\d)(?:([.:])(\d{1,3}))?>")

# [ti:...] [ar:...] [offset:-500] 等元信息标签
_META_TAG = re.compile(r"\[([A-Za-z_]+):([^\]]*)\]")

# QQ / 其他来源的占位文案（整行等于或以之开头时视为占位）
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "纯音乐，请欣赏",
    "纯音乐,请欣赏",
    "纯音乐请欣赏",
    "此歌曲为没有填词的纯音乐",
    "该歌曲为纯音乐",
    "本歌曲为纯音乐",
    "纯音乐",
    "暂无歌词",
    "歌词暂无",
    "暂无歌词，请欣赏",
    "instrumental",
    "no lyrics",
)

def decode_lyrics_bytes(raw: bytes) -> tuple[str, bool]:
    """解码歌词字节，返回 (文本, 是否带 BOM)。

    优先级：UTF-8(带/不带 BOM) → UTF-16 → GB18030。
    真实曲库中存在 GBK/GB18030 歌词，不能只试 UTF-8。
    """
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        try:
            return raw.decode("utf-8-sig"), True
        except UnicodeDecodeError:
            pass

    # **UTF-16 必须先看 BOM 再试。**
    # UTF-16 几乎能"成功"解码任意偶数长度的字节串（包括 GBK 中文），
    # 结果是满屏私用区字符。它排在 gb18030 前面时，会把 GBK 歌词解成乱码，
    # 而乱码不抛异常——于是「读到了，但全是乱码」。实际两份 30 行的
    # 同一首歌歌词，因此被判成「共有行 0」。
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), has_bom
        except UnicodeError:
            pass

    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # gb18030 几乎不会抛错，容易把任意字节解成乱码；
        # 用不可打印字符比例做一次粗筛，避免把二进制噪声当歌词。
        if enc == "gb18030" and _looks_like_noise(text):
            continue
        return text, has_bom
    return "", has_bom


def _looks_like_noise(text: str) -> bool:
    if not text:
        return False
    bad = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return bad / max(len(text), 1) > 0.05


def _fraction_to_ms(sep: str | None, fraction: str | None) -> int:
    """小数部分转毫秒。

    '.' 表示小数：1 位 = 百毫秒，2 位 = 十毫秒，3 位 = 毫秒。
    ':' 表示毫秒直读：[mm:ss:xx] 按毫秒左对齐到 3 位。
    """
    if not fraction:
        return 0
    if sep == ":":
        return int(fraction.ljust(3, "0")[:3])
    digits = len(fraction)
    if digits == 1:
        return int(fraction) * 100
    if digits == 2:
        return int(fraction) * 10
    return int(fraction[:3])


def _iter_timestamps(line: str) -> Iterator[int]:
    for m in _TIME_TAG.finditer(line):
        minutes, seconds, sep, frac = m.group(1), m.group(2), m.group(3), m.group(4)
        yield int(minutes) * 60_000 + int(seconds) * 1_000 + _fraction_to_ms(sep, frac)


def is_placeholder_only(text_lines: Iterable[str]) -> bool:
    """判断歌词是否只由占位文案构成。"""
    meaningful = [ln.strip() for ln in text_lines if ln.strip()]
    if not meaningful:
        return False
    for line in meaningful:
        low = line.lower()
        if not any(low.startswith(marker) or marker in low for marker in PLACEHOLDER_MARKERS):
            return False
    return True


def _iter_word_timestamps(line: str) -> Iterator[int]:
    """逐字标签 <mm:ss.xx> 的时间戳。"""
    for m in _WORD_TAG.finditer(line):
        minutes, seconds, sep, frac = m.group(1), m.group(2), m.group(3), m.group(4)
        yield int(minutes) * 60_000 + int(seconds) * 1_000 + _fraction_to_ms(sep, frac)


def parse_lrc(
    text: str,
    *,
    source_label: str = "",
    duration_seconds: float | None = None,
) -> ParsedLyrics:
    """解析歌词文本并分类，附带规范化内容指纹（normalized_hash）。"""
    parsed = _parse_lrc_inner(
        text, source_label=source_label, duration_seconds=duration_seconds
    )
    if parsed.state is LyricsState.ABSENT or not text.strip():
        return parsed
    return replace(parsed, normalized_hash=content_hash(text))


def _parse_lrc_inner(
    text: str,
    *,
    source_label: str = "",
    duration_seconds: float | None = None,
) -> ParsedLyrics:
    """解析歌词文本并分类。

    返回 ParsedLyrics，其中 state 为 absent / plain_text / synced /
    synced_suspicious 之一。解码失败应在调用前处理为 unparseable。
    """
    if text is None or not text.strip():
        return ParsedLyrics(state=LyricsState.ABSENT)

    meta: dict[str, str] = {}
    entries: list[tuple[int, str]] = []  # (start_ms, text)
    timestamp_count = 0
    has_word_level = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 元信息标签（ti/ar/al/by/offset/...）——含非数字键名的 [xx:yy]
        line_without_time = _TIME_TAG.sub("", line)
        for m in _META_TAG.finditer(line_without_time):
            key, value = m.group(1).lower(), m.group(2).strip()
            # 只把非纯数字键当元信息，避免误吞 [00:12] 残留
            if not key.isdigit():
                meta.setdefault(key, value)

        word_stamps = list(_iter_word_timestamps(line))
        if word_stamps:
            has_word_level = True

        stamps = list(_iter_timestamps(line))
        if not stamps and word_stamps:
            # 只有逐字标签、没有方括号行标签的增强型 LRC。
            # 若不把逐字时间戳当作行时间戳，整首歌会丢失时间轴并退化成
            # plain_text —— 而 plain_text 正是「清理无时间轴歌词」的输入，
            # 会进一步触发错误的清理决策。
            stamps = word_stamps[:1]

        # 去掉所有标签（时间轴 / 逐字 / 元信息）后剩下的才是歌词正文。
        # 必须一并剥离元信息标签，否则 [ti:...] 会被当成歌词文本，
        # 导致「只有元信息、没有正文」的歌曲被误判为 plain_text。
        payload = _META_TAG.sub("", _WORD_TAG.sub("", _TIME_TAG.sub("", line))).strip()

        if stamps:
            timestamp_count += len(stamps)
            for ts in stamps:
                entries.append((ts, payload))
        elif payload:
            entries.append((-1, payload))

    if not entries:
        # 只有元信息、没有歌词正文
        return ParsedLyrics(state=LyricsState.ABSENT, tags=meta)

    timed = [(ts, txt) for ts, txt in entries if ts >= 0]
    untimed = [txt for ts, txt in entries if ts < 0]
    has_text = any(txt for _, txt in entries)

    problems: list[str] = []
    offset_ms = 0
    if "offset" in meta:
        try:
            offset_ms = int(meta["offset"])
        except ValueError:
            problems.append(f"invalid_offset:{meta['offset']}")

    if not has_text:
        # 有时间轴但没有正文
        if timed:
            problems.append("timeline_without_text")
            return ParsedLyrics(
                state=LyricsState.SYNCED_SUSPICIOUS,
                timestamp_count=timestamp_count,
                offset_ms=offset_ms,
                has_word_level=has_word_level,
                tags=meta,
                problems=tuple(problems),
            )
        return ParsedLyrics(state=LyricsState.ABSENT, tags=meta)

    if not timed:
        # 纯粹的文本歌词
        text_lines = [t for t in untimed if t.strip()]
        if is_placeholder_only(text_lines):
            problems.append("placeholder_only")
            # 占位文案是**准确的状态**，不是异常：这首歌本来就没有歌词。
            return ParsedLyrics(
                state=LyricsState.PLACEHOLDER,
                line_count=len(text_lines),
                tags=meta,
                problems=tuple(problems),
            )
        return ParsedLyrics(
            state=LyricsState.PLAIN_TEXT,
            line_count=len(text_lines),
            tags=meta,
            problems=tuple(problems),
        )

    # ---- 有时间轴 ----
    text_lines = [txt for ts, txt in timed if txt]
    first_ms = min(ts for ts, _ in timed)
    last_ms = max(ts for ts, _ in timed)
    effective_first = first_ms + offset_ms
    effective_last = last_ms + offset_ms

    if first_ms == 0 and last_ms == 0:
        problems.append("all_timestamps_zero")
    if timestamp_count > 0 and len(text_lines) == 0:
        problems.append("no_text_on_timeline")
    if is_placeholder_only(text_lines):
        problems.append("placeholder_only")
    if duration_seconds and duration_seconds > 0:
        limit_ms = int(duration_seconds * 1000) + 5_000
        if effective_last > limit_ms:
            problems.append(f"timestamp_beyond_duration:{effective_last}>{limit_ms}")
    if effective_first < 0:
        problems.append("negative_effective_timestamp")
    if len(text_lines) <= 1:
        # 不直接判为无歌词，只标记待审查
        problems.append("low_text_line_count")

    hard_problems = [
        p
        for p in problems
        if p.startswith(("all_timestamps_zero", "placeholder_only", "timestamp_beyond_duration",
                         "no_text_on_timeline"))
    ]

    # 整份歌词只有占位文案 → 这是「这首歌没有歌词」的**准确描述**，
    # 不是时间轴异常。单独给一个状态，别混进「可疑」里。
    if is_placeholder_only(text_lines):
        return ParsedLyrics(
            state=LyricsState.PLACEHOLDER,
            line_count=len(text_lines),
            timestamp_count=timestamp_count,
            offset_ms=offset_ms,
            has_word_level=has_word_level,
            first_timestamp_ms=effective_first,
            last_timestamp_ms=effective_last,
            tags=meta,
            problems=tuple(problems),
        )

    return ParsedLyrics(
        state=LyricsState.SYNCED_SUSPICIOUS if hard_problems else LyricsState.SYNCED,
        line_count=len(text_lines),
        timestamp_count=timestamp_count,
        offset_ms=offset_ms,
        has_word_level=has_word_level,
        first_timestamp_ms=effective_first,
        last_timestamp_ms=effective_last,
        tags=meta,
        problems=tuple(problems),
    )


def normalize_lrc(text: str, *, drop_word_level: bool = True) -> str:
    """规范化为逐行 LRC：UTF-8 无 BOM、LF、行级时间轴。

    - 逐字标签（<mm:ss.xx>）默认降级为行级，只保留该行第一个时间戳
    - 保留 offset 元信息但把 offset 落到显式时间戳上，避免播放器重复应用
    - 同一行多个时间戳展开为多行（LRC 标准语义）
    """
    out_lines: list[str] = []
    meta: dict[str, str] = {}
    offset_ms = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        _TIME_TAG.sub("", _WORD_TAG.sub("", line)).strip()
        for m in _META_TAG.finditer(_TIME_TAG.sub("", line)):
            key, value = m.group(1).lower(), m.group(2).strip()
            if not key.isdigit():
                meta.setdefault(key, value)
        if meta.get("offset"):
            try:
                offset_ms = int(meta["offset"])
            except ValueError:
                offset_ms = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = _META_TAG.sub("", _WORD_TAG.sub("", _TIME_TAG.sub("", line))).strip()
        stamps = list(_iter_timestamps(line))
        if not stamps:
            # 同样处理「只有逐字标签」的形态，否则规范化会把时间轴整个丢掉
            word_stamps = list(_iter_word_timestamps(line))
            if word_stamps:
                stamps = word_stamps[:1]
        if not stamps:
            if payload:
                out_lines.append(payload)
            continue
        for ts in stamps:
            applied = max(ts + offset_ms, 0)
            out_lines.append(f"[{_format_ts(applied)}]{payload}")

    # 去掉 offset 元信息行（已落到时间戳上）
    kept = [ln for ln in out_lines if not re.fullmatch(r"\[offset:[-+]?\d+\]", ln.strip(), re.I)]
    body = "\n".join(kept).strip()
    normalized = (body + "\n") if body else ""
    # 逐行输出不应含 <...> 逐字标签
    if drop_word_level:
        normalized = _WORD_TAG.sub("", normalized)
    return normalized


def _format_ts(ms: int) -> str:
    minutes, rem = divmod(max(ms, 0), 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def format_ms(ms: int) -> str:
    """毫秒 → `mm:ss.xx`，用于 UI 预览显示。"""
    if ms < 0:
        return ""
    minutes, rem = divmod(ms, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


@dataclass(frozen=True, slots=True)
class LrcLine:
    ms: int
    """时间戳（毫秒）；-1 表示这一行没有时间戳。"""

    text: str

    @property
    def stamp(self) -> str:
        return format_ms(self.ms)


def parse_lrc_lines(text: str) -> list[LrcLine]:
    """把歌词拆成逐行 (时间, 文本)，供 UI 预览与逐行对齐显示。

    与 `parse_lrc` 的区别：这里不做分类判断，只负责把内容摊开成便于展示的结构。
    一行有多个时间戳时展开成多行——这是 LRC 的标准语义（同一句重复出现）。
    """
    out: list[LrcLine] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        payload = _META_TAG.sub("", _WORD_TAG.sub("", _TIME_TAG.sub("", line))).strip()
        stamps = list(_iter_timestamps(line))
        if not stamps:
            word_stamps = list(_iter_word_timestamps(line))
            if word_stamps:
                stamps = word_stamps[:1]
        if not stamps:
            if payload:
                out.append(LrcLine(-1, payload))
            continue
        for ts in stamps:
            out.append(LrcLine(ts, payload))
    return out


def merge_bilingual(text: str, translation: str, *, tolerance_ms: int = 800) -> str:
    """把独立通道的翻译合并成双语文体：每条原文时间轴行下面紧跟一条翻译行。

    背景：QQ（trans 字段）与网易云（tlyric）返回的翻译是**独立整段**文本，
    自带与原文平行的时间轴。合并规则：
    - 以原文行为主轴；每条原文行在翻译里找时间轴最近的行，差 ≤ tolerance_ms 才配对；
    - 翻译行输出时**沿用原文行的时间轴**（播放器按时间滚动时双语紧邻显示）；
    - 一条翻译行只消费一次（原文里两条时间轴相同/相近的行不共享同一条翻译）；
    - 配不上翻译的原文行原样保留；配不上原文的翻译行**丢弃**；
    - 无时间轴的原文行与元信息行（[ti:…] 等）原样保留在原位置；
    - 输出按原文行序排列；空输入/无时间轴输入直接返回原文。
    """
    # 翻译侧没有带时间轴的行（空文本 / 纯文本 / 只有元信息）时无从配对。
    # 这里只做行级配对，不经过 parse_lrc 的状态判定——占位词/可疑与否与本函数无关。
    trans_timed = [ln for ln in parse_lrc_lines(translation) if ln.ms >= 0]
    if not trans_timed:
        return text

    used = [False] * len(trans_timed)

    def _take_nearest(ms: int) -> LrcLine | None:
        """取未消费翻译行里时间轴最近且差 ≤ tolerance_ms 的一行，命中即标记消费。

        按原文行序贪心消费：时间轴相同/相近的多条原文行，只有靠前的那条拿到翻译。
        """
        best = -1
        best_diff = 0
        for i, ln in enumerate(trans_timed):
            if used[i]:
                continue
            diff = abs(ln.ms - ms)
            if diff > tolerance_ms:
                continue
            if best < 0 or diff < best_diff:
                best, best_diff = i, diff
        if best < 0:
            return None
        used[best] = True
        return trans_timed[best]

    out_lines: list[str] = []
    paired = 0
    for raw_line in text.splitlines():
        # 与 parse_lrc_lines 相同的时间轴识别：行级标签优先，没有时回退逐字标签首戳。
        stamps = list(_iter_timestamps(raw_line))
        if not stamps:
            stamps = list(_iter_word_timestamps(raw_line))[:1]
        if not stamps:
            # 元信息行（[ti:…] 等）、无时间轴行与空行：原样保留在原位置。
            out_lines.append(raw_line)
            continue
        out_lines.append(raw_line)
        matched = _take_nearest(stamps[0])
        if matched is not None:
            # 翻译行沿用原文行的时间轴而不是翻译自己的；多时间戳行按首戳配对。
            out_lines.append(f"[{format_ms(stamps[0])}]{matched.text}")
            paired += 1

    if not paired:
        # 一条都没配上（含原文没有时间轴的情形）：完全不改写，避免
        # splitlines/join 悄悄抹掉 CRLF 行尾或原有空白。
        return text
    merged = "\n".join(out_lines)
    if text.endswith("\n"):
        merged += "\n"
    return merged


def content_hash(text: str) -> str:
    """规范化内容的指纹，用于幂等比较（不用于判断内容正确性）。"""
    canonical = normalize_lrc(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
