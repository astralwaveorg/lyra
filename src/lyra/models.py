"""Lyra 领域模型与状态枚举。

架构约束（见 docs/architecture.md）：
- 状态必须可解释且互相分离：歌词状态、排除状态、来源状态、任务状态、事务状态
  不要合成一个「大状态」。
- 歌词状态与「文件格式」正交：一个 FLAC 文件可以是 absent / plain_text / synced。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

__all__ = [
    "RootRole",
    "MediaFormat",
    "LyricsState",
    "LyricsOrigin",
    "IssueCode",
    "ParsedLyrics",
    "AudioInfo",
    "EmbeddedLyricsFinding",
    "SidecarFinding",
    "TrackAudit",
    "AuditSummary",
]


class RootRole(StrEnum):
    """受管根的角色。初版只用 LIBRARY；INBOX 为后续版本整理流水线预留。"""

    LIBRARY = "library"
    INBOX = "inbox"


_CANONICAL_EXTENSION: dict[str, str] = {
    "flac": ".flac",
    "ogg_vorbis": ".ogg",
    "ogg_opus": ".opus",
    "mp3": ".mp3",
}


class MediaFormat(StrEnum):
    """真实容器格式。注意：以文件内容判定，不以扩展名判定。"""

    FLAC = "flac"
    OGG_VORBIS = "ogg_vorbis"
    OGG_OPUS = "ogg_opus"
    MP3 = "mp3"
    UNSUPPORTED = "unsupported"

    @property
    def supported(self) -> bool:
        return self is not MediaFormat.UNSUPPORTED

    @property
    def canonical_extension(self) -> str:
        """该容器应当使用的扩展名。

        两个用途：修正「扩展名与容器不符」（实际库里有 2 个真身是 OGG 却叫
        .flac 的文件），以及替换上传时**按真实内容**决定目标文件名——
        用户上传 def.mp3 去替换 abc.flac 时，正确结果是 abc.mp3。
        """
        return _CANONICAL_EXTENSION.get(self, "")


class LyricsState(StrEnum):
    """歌词的时间轴状态。这是「格式检查」结论，不等于内容正确。"""

    ABSENT = "absent"
    """没有歌词，或歌词为空 / 纯空白。"""

    PLAIN_TEXT = "plain_text"
    """有无时间轴的歌词文本。"""

    SYNCED = "synced"
    """含可解析时间轴。"""

    SYNCED_SUSPICIOUS = "synced_suspicious"
    """有时间轴但明显异常（占位文案、全零时间、越界等）。保留并复核，不当纯文本删。"""

    UNPARSEABLE = "unparseable"
    """编码或结构无法解析。不修改。"""


class LyricsOrigin(StrEnum):
    """歌词来自哪里。"""

    NONE = "none"
    VORBIS_COMMENT = "vorbis_comment"
    ID3_USLT = "id3_uslt"
    ID3_SYLT = "id3_sylt"
    SIDECAR = "sidecar"


class IssueCode(StrEnum):
    """库健康检查的问题码。"""

    EMPTY_SIDECAR = "empty_sidecar"
    ORPHAN_SIDECAR = "orphan_sidecar"
    DUPLICATE_SIDECAR = "duplicate_sidecar"
    SIDECAR_NAME_MISMATCH = "sidecar_name_mismatch"
    """同名匹配失败，但规范化文件名主体后可唯一配对（如 `01 X` ↔ `1. X`）。"""
    SIDECAR_AMBIGUOUS = "sidecar_ambiguous"
    """同目录下存在多个规范化主体相同的 lrc，无法安全断定归属，不自动配对。"""
    SIDECAR_BOM = "sidecar_bom"
    SIDECAR_UNPARSEABLE = "sidecar_unparseable"
    CONTAINER_MISMATCH = "container_mismatch"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNPARSEABLE_AUDIO = "unparseable_audio"
    NO_EMBEDDED_COVER = "no_embedded_cover"
    NO_COVER_ARTWORK = "no_cover_artwork"
    MISSING_MBID = "missing_mbid"
    MISSING_ISRC = "missing_isrc"
    MISSING_TITLE = "missing_title"
    MISSING_ARTIST = "missing_artist"
    MISSING_ALBUM = "missing_album"
    MISSING_GENRE = "missing_genre"
    MISSING_SIDECAR = "missing_sidecar"
    MISSING_EMBEDDED = "missing_embedded"
    NO_LYRICS = "no_lyrics"
    PLAIN_EMBEDDED_LYRICS = "plain_embedded_lyrics"
    SUSPICIOUS_LYRICS = "suspicious_lyrics"
    LYRICS_MISMATCH = "lyrics_mismatch"


@dataclass(frozen=True, slots=True)
class ParsedLyrics:
    """一次歌词解析的结果。"""

    state: LyricsState
    line_count: int = 0
    """有实际文本的歌词行数。"""

    timestamp_count: int = 0
    offset_ms: int = 0
    has_word_level: bool = False
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    """人类可读的问题描述，用于报告与复核。"""

    normalized_hash: str = ""
    """规范化后内容的指纹。

    用于判断「内嵌与 sidecar 内容是否一致」以及写入幂等比较。
    **不得用于判断歌词内容是否正确。**
    """


@dataclass(slots=True)
class AudioInfo:
    """一个音频文件的基本事实。"""

    path: Path
    rel_path: str
    """相对受管根的精确相对路径（原样，不做 Unicode 规范化）。"""

    extension: str
    """文件系统上的扩展名（小写，含点）。"""

    media_format: MediaFormat
    duration: float | None = None
    size: int = 0
    mtime: float = 0.0
    ctime: float = 0.0
    inode: int = 0
    """文件系统的 inode 号。**改名后不变**，是识别「同一个文件换了路径」最可靠的依据。

    早先库里这一列永远是 NULL——`upsert_location` 接受 inode 参数，但没有任何
    调用方传过它（实际 `SELECT COUNT(*) FROM locations WHERE inode IS NOT NULL`
    为 0）。于是「按 inode 识别改名」那段逻辑从未执行过：同目录内换文件夹能
    识别，只是因为文件名没变、走了 `(文件名, 大小)` 兜底；**改了文件名就识别不到**。
    """
    parse_error: str | None = None

    @property
    def extension_matches_container(self) -> bool:
        """扩展名是否与真实容器一致。用于报告，不作为处理前提。"""
        ext = self.extension
        fmt = self.media_format
        if fmt is MediaFormat.FLAC:
            return ext == ".flac"
        if fmt is MediaFormat.MP3:
            return ext == ".mp3"
        if fmt in (MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
            return ext in (".ogg", ".oga", ".opus")
        return False


@dataclass(slots=True)
class EmbeddedLyricsFinding:
    """内嵌歌词的观察结果。"""

    state: LyricsState = LyricsState.ABSENT
    origin: LyricsOrigin = LyricsOrigin.NONE
    fields: tuple[str, ...] = ()
    """命中的歌词字段名（白名单内的）。"""

    parsed: ParsedLyrics | None = None
    has_cover: bool = False


@dataclass(slots=True)
class SidecarFinding:
    """同名 .lrc 的观察结果。"""

    exists: bool = False
    path: Path | None = None
    size: int = 0
    state: LyricsState = LyricsState.ABSENT
    has_bom: bool = False
    parsed: ParsedLyrics | None = None

    @property
    def is_effectively_absent(self) -> bool:
        """0 字节或纯空白视为不存在，可安全填充。

        这是对 架构文档中的修订 2：规范只说「不覆盖已有不同内容的 LRC」，
        但 0 字节文件没有内容可冲突。不这样定义，曲库 178 个空 LRC
        会被判成「已有歌词」而全部拒绝写入。
        """
        if not self.exists:
            return True
        return self.state is LyricsState.ABSENT


@dataclass(slots=True)
class TrackAudit:
    """单曲的审计结论。"""

    audio: AudioInfo
    embedded: EmbeddedLyricsFinding = field(default_factory=EmbeddedLyricsFinding)
    sidecar: SidecarFinding = field(default_factory=SidecarFinding)
    tags: dict[str, list[str]] = field(default_factory=dict)
    issues: list[IssueCode] = field(default_factory=list)

    @property
    def effective_state(self) -> LyricsState:
        """综合内嵌与 sidecar 后的有效歌词状态。

        优先级：sync状态优先于 plain，plain 优先于 absent。
        suspicious 与 unparseable 单独保留，便于复核。
        """
        states = {self.embedded.state, self.sidecar.state}
        if LyricsState.UNPARSEABLE in states:
            return LyricsState.UNPARSEABLE
        if LyricsState.SYNCED_SUSPICIOUS in states:
            return LyricsState.SYNCED_SUSPICIOUS
        if LyricsState.SYNCED in states:
            return LyricsState.SYNCED
        if LyricsState.PLAIN_TEXT in states:
            return LyricsState.PLAIN_TEXT
        return LyricsState.ABSENT


@dataclass(slots=True)
class AuditSummary:
    """库级审计汇总。"""

    root: str = ""
    tracks_total: int = 0
    by_format: dict[str, int] = field(default_factory=dict)
    by_lyrics_state: dict[str, int] = field(default_factory=dict)
    by_issue: dict[str, int] = field(default_factory=dict)
    sidecar_total: int = 0
    sidecar_empty: int = 0
    tracks_without_lyrics: int = 0
    parse_failures: int = 0
    duration_seconds: float = 0.0
    issues_skipped_unfixable: int = 0
    """因为**格式不支持改写标签**而没有上报的缺陷条数。

    只有 FLAC 能改标签（见 `audit.TAG_WRITABLE_FORMATS`）。对 OGG/MP3 报
    「缺 MusicBrainz ID」只会得到一个必然失败的动作，所以不计入问题列表，
    但也不该悄悄消失——界面上单独说明「另有 N 项因格式不支持而无法修复」。
    """
