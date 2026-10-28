"""文件名解析：从音频文件名提取标题、艺人等元数据线索。

文件名是所有元数据来源里最脏的一种：编号写法（`01 X` / `1-05 X` / `02. X`）、
括号习惯（`(Live)` / `[feat. X]` / `（2006）`）、分隔符习惯（` - ` / `—`）
因人而异，而大量文件又只有文件名可用。因此解析产物只是**线索**（hints）
而不是结论：每项都带置信度，由上层（匹配、提案）自行决定采信与否。

设计约束：

1. **与 scan.py 的配对逻辑共用同一个前导编号正则。** `normalize_stem` /
   `track_number_hint` 对「什么是编号」的判断必须与这里一致，否则同一文件
   会在两处解析出不同的编号——scan.py 注释里记录过 `1-05` 只剥一层的真实
   事故，复制一份正则迟早漂移。
2. **比较用归一化，保存用原文。** 目录名与艺人名的相等判断走
   `match.normalize_text`（繁简/全角/casefold），但 hints 里保留用户写的
   原文，不悄悄改写。
3. **版本词表不在此维护。** 括号内容的版本识别复用
   `match.extract_version_tokens`，词表只改一处。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .match import extract_version_tokens, normalize_text, split_artists
from .scan import _LEADING_TRACK_NO

__all__ = [
    "FilenameHints",
    "parse_filename",
]


@dataclass(frozen=True, slots=True)
class FilenameHints:
    """从文件名解析出的线索。字符串一律保存原文，不做归一化改写。"""

    title: str
    title_conf: float  # 0.0~1.0
    artists: tuple[str, ...]
    artists_conf: float
    album: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    #: 规范化的版本标签（如 "live"），来自 match 的版本词表
    version_tokens: tuple[str, ...] = ()
    #: feat/ft 括号里的合作艺人
    feat: tuple[str, ...] = ()


#: 括号段：括号族与 match._BRACKETED 保持一致（含全角与直角引号），
#: 保证这里的折叠与 strip_bracketed 的「基标题」口径相同。
_BRACKET_GROUP = re.compile(r"[\(\[（【「]\s*([^\)\]）】」]*)\s*[\)\]）】」]")

#: feat/ft 开头的括号内容。必须有词边界：`(feature film)` 不是合作艺人。
_FEAT_HEAD = re.compile(r"(?:feat|ft)\b\.?\s*", re.IGNORECASE)

#: 「 - 」分隔：普通连字符（含全角－）必须两侧都有空白才认定，`AC-DC` 不拆。
_SPACE_DASH = re.compile(r"\s+[-－]\s+")

#: 破折号/连接号：中文文件名常不带空格，出现即视为分隔。
_DASH = re.compile(r"\s*[–—]\s*")

#: 拆分前清掉两端悬挂的分隔符（`- 歌名`、`歌名 -`、`---`）。
_DASH_CHARS = " -－–—"


def parse_filename(stem: str, *, parent_dir: str = "") -> FilenameHints:
    """解析文件名主体（不含扩展名），返回元数据线索。

    `parent_dir` 是所在目录名：规范化后与解析出的艺人名相等时提高艺人
    置信度——「艺人/艺人 - 歌名」是曲库里最可靠的目录组织习惯。
    """
    raw = _last_segment(stem)

    # 1. 前导编号：`01`、`01.`、`01 -`、`1-05 -`；多段时最后一段是音轨号，
    #    倒数第二段是碟号。纯数字开头的真实标题（`99 Luftballons`）会被
    #    误当编号——这是复用 scan 正则的自觉取舍，两边行为保持一致。
    track_no: int | None = None
    disc_no: int | None = None
    body = raw
    lead = _LEADING_TRACK_NO.match(raw)
    if lead:
        numbers = re.findall(r"\d+", lead.group(0))
        if numbers:
            track_no = int(numbers[-1])
            if len(numbers) >= 2:
                disc_no = int(numbers[-2])
        body = raw[lead.end():]

    # 2. 括号折叠：版本/feat/纯数字取出，其余文本原位并回主干。
    folded, versions, feats, title_markers = _fold_brackets(body)

    # 3. 主干拆分。
    title, title_conf, artists, artists_conf, album = _split_backbone(
        body, folded, parent_dir, title_markers,
    )
    return FilenameHints(
        title=title,
        title_conf=title_conf,
        artists=artists,
        artists_conf=artists_conf,
        album=album,
        track_no=track_no,
        disc_no=disc_no,
        version_tokens=versions,
        feat=feats,
    )


def _last_segment(stem: str) -> str:
    """容错：调用方可能把带目录的路径整个传来，只取最后一段。"""
    if not stem:
        return ""
    return stem.replace("\\", "/").rsplit("/", 1)[-1]


def _fold_brackets(
    body: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...], list[int]]:
    """把括号段从主干取出分类。

    返回 (折叠后的主干, 版本标签, feat 艺人, 标题侧标记位置)。
    版本/feat/纯数字括号整体拿掉；未知文本原位保留（视作标题一部分）。
    标题侧标记位置用于识别「标题 (版本) - 艺人」这种版本在前的少见形态：
    版本和 feat 两类括号只挂在标题上，它们出现在分隔符哪一侧，
    哪一侧就是标题。
    """
    versions: list[str] = []
    feats: list[str] = []
    markers: list[int] = []

    def classify(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return " "
        head = _FEAT_HEAD.match(inner)
        if head:
            feats.extend(split_artists(inner[head.end():]))
            markers.append(match.start())
            return " "
        tokens = extract_version_tokens(inner)
        if tokens:
            versions.extend(sorted(tokens))
            markers.append(match.start())
            return " "
        if inner.isdigit():
            return " "  # 年份或其它纯数字：既非标题也非版本，丢弃
        return f" {inner} "  # 未知文本：原位并回，当作标题的一部分

    folded = _BRACKET_GROUP.sub(classify, body)
    return folded, _dedupe(versions), _dedupe(feats), markers


def _split_backbone(
    body: str,
    folded: str,
    parent_dir: str,
    title_markers: Sequence[int],
) -> tuple[str, float, tuple[str, ...], float, str]:
    """按分隔符拆主干，返回 (title, title_conf, artists, artists_conf, album)。

    短路顺序：恰好两个 ` - ` → 艺人-专辑-歌名；恰好一个 → 艺人-歌名；
    分隔符缺失或过多（>=3 段分不清谁是艺人）→ 整段当标题，不猜。
    """
    text = " ".join(folded.split()).strip(_DASH_CHARS)
    if not text:
        return "", 0.0, (), 0.0, ""
    parts = _split_segments(text, _SPACE_DASH)
    if len(parts) == 3:
        return parts[2], 0.7, (parts[0],), 0.6, parts[1]
    if len(parts) == 2:
        return _two_way(parts[0], parts[1], parent_dir, _title_on_left(body, title_markers))
    parts = _split_segments(text, _DASH)
    if len(parts) == 2:
        return _two_way(parts[0], parts[1], parent_dir, _title_on_left(body, title_markers))
    return text, 0.6, (), 0.0, ""


def _split_segments(text: str, pattern: re.Pattern[str]) -> list[str]:
    """按分隔符拆段：剥掉段内残留的悬挂分隔符，再丢弃空段。

    连续分隔符（`A - - B`）的共享空白会被上一段吞掉，第二段因此带着
    前导 `-`，这里兜底剥掉，避免拆出 `- B` 这样的标题。
    """
    return [seg for seg in (p.strip(_DASH_CHARS) for p in pattern.split(text)) if seg]


def _title_on_left(body: str, title_markers: Sequence[int]) -> bool:
    """标题侧标记（版本/feat 括号）是否只在第一个分隔符左侧。

    `晴天 (Live) - 周杰伦` 这种「版本在前」的形态里，括号挂在标题上；
    左侧有标记而右侧没有时按「标题 - 艺人」读，而不是默认的「艺人 - 标题」。
    """
    if not title_markers:
        return False
    sep = _SPACE_DASH.search(body) or _DASH.search(body)
    if sep is None:
        return False
    left = any(start < sep.start() for start in title_markers)
    right = any(start >= sep.end() for start in title_markers)
    return left and not right


def _two_way(
    left: str,
    right: str,
    parent_dir: str,
    title_on_left: bool,
) -> tuple[str, float, tuple[str, ...], float, str]:
    """`艺人 - 歌名` 两段形态；默认前段是艺人，可被标题侧标记反转。"""
    artist_seg, title_seg = (right, left) if title_on_left else (left, right)
    # 目录名旁证只在相等（归一化后）时采信：包含关系的目录名
    # （`张韶涵 - 潘朵拉 (2006)`）不能当艺人证据，更不能当专辑来源。
    if parent_dir and normalize_text(parent_dir) == normalize_text(artist_seg):
        return title_seg, 0.85, (artist_seg,), 0.9, ""
    return title_seg, 0.8, (artist_seg,), 0.6, ""


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    """去重但保持首次出现的顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)
