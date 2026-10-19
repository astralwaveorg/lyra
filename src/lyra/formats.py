"""格式识别与标签读取。

两条不可违反的规则：

1. **以真实容器为准，不按扩展名判断。** 曲库中已发现 2 个 `*.flac`
   实际是 OGG 容器。按扩展名判断会让解析直接抛错或漏读。

2. **歌词字段必须用精确白名单，禁止 `"lyric" in field_name`。**
   `LYRICIST`（作词人）与 ID3 的 `TEXT` 帧是作词人信息，**不是歌词**，
   绝不能被清理。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, USLT
from mutagen.mp3 import MP3
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .lrc import parse_lrc
from .models import (
    AudioInfo,
    EmbeddedLyricsFinding,
    LyricsOrigin,
    LyricsState,
    MediaFormat,
)

__all__ = [
    "AudioMeta",
    "VORBIS_LYRICS_FIELDS",
    "ID3_LYRICS_FRAMES",
    "AUDIO_EXTENSIONS",
    "sniff_container",
    "detect_media_format",
    "read_audio_meta",
    "read_embedded_lyrics",
    "read_embedded_lyrics_texts",
]

# ---------------------------------------------------------------------------
# 歌词字段白名单（唯一权威定义，读与写共用）
# ---------------------------------------------------------------------------

#: Vorbis comment 中的歌词字段（大小写不敏感）。
#: 注意 LYRICIST 不在其中——它是作词人。
VORBIS_LYRICS_FIELDS: frozenset[str] = frozenset({"LYRICS", "UNSYNCEDLYRICS"})

#: ID3 中的歌词帧。USLT = 非同步歌词，SYLT = 同步歌词。
#: 注意 TEXT（作词人）与 TCOM（作曲）不在其中。
ID3_LYRICS_FRAMES: tuple[str, ...] = ("USLT", "SYLT")

#: 明确不是歌词、必须保护的字段（用于自检与文档）
NON_LYRICS_LOOKALIKES: frozenset[str] = frozenset(
    {"LYRICIST", "TEXT", "TCOM", "COMPOSER", "SONGWRITER", "WRITER"}
)

#: 参与识别与处理的容器扩展名（真正判定仍看内容）
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".wav", ".ape", ".wv", ".aiff"}
)

#: 本版本支持写入歌词的格式
SUPPORTED_WRITE_FORMATS: frozenset[MediaFormat] = frozenset(
    {MediaFormat.FLAC, MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS, MediaFormat.MP3}
)

# 用于匹配与审计的标签键（不限于歌词）
_MATCH_TAG_KEYS: tuple[str, ...] = (
    "TITLE", "ARTIST", "ARTISTS", "ALBUM", "ALBUMARTIST", "ALBUMARTISTS",
    "DATE", "GENRE", "TRACKNUMBER",
    "DISCNUMBER", "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_RELEASEGROUPID",
    "ISRC", "ACOUSTID_ID", "COMPOSER", "LYRICIST", "LABEL",
)


@dataclass(slots=True)
class AudioMeta:
    """音频文件的读取结果。"""

    info: AudioInfo
    tags: dict[str, list[str]]
    """匹配相关标签。键为大写。"""

    embedded: EmbeddedLyricsFinding


#: 嗅探所需的头部字节数。OGG 的编解码标识在第一个页面里，
#: 4 KiB 足以覆盖（含较长的 vendor 字符串）。
_SNIFF_BYTES = 4096

#: OGG 容器内的编解码标识。顺序重要：OpusHead 必须先判，
#: 否则 Opus 文件 tags 里的 "vorbis" 字样会被误判为 Vorbis。
_OGG_CODEC_MARKERS: tuple[tuple[bytes, MediaFormat], ...] = (
    (b"OpusHead", MediaFormat.OGG_OPUS),
    (b"\x01vorbis", MediaFormat.OGG_VORBIS),
    (b"vorbis", MediaFormat.OGG_VORBIS),
)

#: Ogg FLAC（容器是 OGG、数据是 FLAC）。本版本不支持其歌词写入，明确识别出来。
_OGGFLAC_MARKER = b"\x7fFLAC"

_CLASS_FOR_FORMAT: dict[MediaFormat, Any] = {}


def sniff_container(path: Path) -> MediaFormat | None:
    """**只看内容**判断容器格式，完全不看扩展名。

    为什么必须自己实现：`mutagen.File()` 的类型打分**依赖扩展名**。
    实际把一个真实的 OGG/Vorbis 文件命名为 `.flac`，`mutagen.File()`
    不去看 `OggS` 头，而是直接抛 `FLACNoHeaderError`，导致该文件被
    判定为「不支持」而整个跳过——用户的歌词就再也写不进去。

    返回 None 表示无法识别（可能不是音频，或容器内码流未知）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_SNIFF_BYTES)
    except OSError:
        return None

    if head[:4] == b"fLaC":
        return MediaFormat.FLAC

    if head[:4] == b"OggS":
        if _OGGFLAC_MARKER in head:
            # OGG 容器 + FLAC 数据。识别得出来，但不支持其歌词写入。
            return MediaFormat.UNSUPPORTED
        for marker, fmt in _OGG_CODEC_MARKERS:
            if marker in head:
                return fmt
        return None

    if head[:3] == b"ID3":
        return MediaFormat.MP3

    # MPEG 帧同步：11 个 1。注意要排除 ID3 已处理的情况。
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return MediaFormat.MP3

    return None


def detect_media_format(path: Path) -> tuple[MediaFormat, Any | None, str | None]:
    """探测真实容器格式。

    返回 (格式, mutagen 对象, 错误)。

    先用 `sniff_container` 按内容定容器，再用**对应的类**显式打开，
    因此 OGG 冒充 `.flac` 会被正确识别，而不是被 mutagen 的扩展名打分坑掉。
    """
    if not _CLASS_FOR_FORMAT:
        _CLASS_FOR_FORMAT.update(
            {
                MediaFormat.FLAC: FLAC,
                MediaFormat.OGG_VORBIS: OggVorbis,
                MediaFormat.OGG_OPUS: OggOpus,
                MediaFormat.MP3: MP3,
            }
        )

    sniffed = sniff_container(path)

    if sniffed is not None and sniffed is not MediaFormat.UNSUPPORTED:
        cls = _CLASS_FOR_FORMAT[sniffed]
        try:
            handle = cls(path)
        except Exception as exc:
            # 容器认得出来但内容读不了 → 保留格式信息，标记解析失败，
            # 这样「扩展名不符」仍能被报告，而不是退化成「未知格式」
            return sniffed, None, f"{type(exc).__name__}: {exc}"
        return sniffed, handle, None

    # 内容嗅探不出（或识别为不支持的 OGG+FLAC）→ 退回 mutagen 的类型打分
    try:
        handle = mutagen.File(path, easy=False)
    except Exception as exc:
        return sniffed or MediaFormat.UNSUPPORTED, None, f"{type(exc).__name__}: {exc}"

    if handle is None:
        return sniffed or MediaFormat.UNSUPPORTED, None, None

    if isinstance(handle, FLAC):
        fmt = MediaFormat.FLAC
    elif isinstance(handle, OggOpus):
        fmt = MediaFormat.OGG_OPUS
    elif isinstance(handle, OggVorbis):
        fmt = MediaFormat.OGG_VORBIS
    elif isinstance(handle, MP3):
        fmt = MediaFormat.MP3
    else:
        return MediaFormat.UNSUPPORTED, handle, None

    return fmt, handle, None


#: ID3v2 文本帧 → 本项目使用的标签名。
#: 只列**已知**的；未知帧按原名保留（见下面的回退分支），
#: 这样自定义帧（TXXX 之类）不会在编辑界面里凭空消失。
_ID3_TEXT_FRAMES = {
    "TIT2": "TITLE",
    "TPE1": "ARTIST",
    "TPE2": "ALBUMARTIST",
    "TALB": "ALBUM",
    "TDRC": "DATE",
    "TYER": "DATE",
    "TRCK": "TRACKNUMBER",
    "TPOS": "DISCNUMBER",
    "TCON": "GENRE",
    "TCOM": "COMPOSER",
    "TEXT": "LYRICIST",
    "TPUB": "LABEL",
    "TCOP": "COPYRIGHT",
    "TSRC": "ISRC",
    "TSOP": "ARTISTSORT",
    "TSO2": "ALBUMARTISTSORT",
}


def _decode_id3_text(body: bytes) -> str:
    """解出 ID3 文本帧的内容。

    帧体第 1 字节是编码：0=Latin-1、1=UTF-16(带 BOM)、2=UTF-16BE、3=UTF-8。
    末尾常有 \x00 终止符，要去掉——否则界面上会显示成一个看不见的方块。
    """
    if not body:
        return ""
    enc = body[0]
    raw = body[1:]
    try:
        if enc == 0:
            text = raw.decode("latin-1", errors="replace")
        elif enc == 1:
            text = raw.decode("utf-16", errors="replace")
        elif enc == 2:
            text = raw.decode("utf-16-be", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 编码损坏不该让整个读取失败
        return ""
    return text.rstrip("\x00").strip()


def _read_id3_tags(path: Path) -> tuple[dict[str, list[str]], str | None]:
    """读取 MP3 的 ID3v2 标签。"""
    from . import id3meta

    try:
        layout = id3meta.id3_parse_file(path)
    except Exception as exc:  # noqa: BLE001
        # 没有 ID3v2 是合法的「空标签」，不是读取失败；写入时再创建。
        if "没有 ID3v2 标签" in str(exc):
            return {}, None
        return {}, f"{type(exc).__name__}: {exc}"

    tags: dict[str, list[str]] = {}
    for frame in layout.frames:
        fid = frame.frame_id
        if fid == "USLT":
            continue          # 歌词走专用字段，不混进通用标签
        if fid == "TXXX":
            parsed = id3meta.parse_txxx(frame.raw[10:])
            if parsed is None:
                continue
            description, value = parsed
            key = id3meta.TXXX_KEYS_BY_DESC.get(description.casefold())
            if key is None or not value:
                continue
            for part in id3meta.split_txxx_values(key, value):
                tags.setdefault(key, []).append(part)
            continue
        if not fid.startswith("T"):
            continue          # 只取文本帧；APIC 等二进制帧不在此列
        # 帧体 = raw[10:]，前 10 字节是帧头
        value = _decode_id3_text(frame.raw[10:])
        if not value:
            continue
        name = _ID3_TEXT_FRAMES.get(fid, fid)
        parts = [part for part in value.split("\x00") if part]
        if name in ("TRACKNUMBER", "DISCNUMBER"):
            number, _, total = parts[0].partition("/")
            if number:
                tags.setdefault(name, []).append(number)
            if total:
                if name == "TRACKNUMBER":
                    tags.setdefault("TRACKTOTAL", []).append(total)
                    tags.setdefault("TOTALTRACKS", []).append(total)
                else:
                    tags.setdefault("DISCTOTAL", []).append(total)
                    tags.setdefault("TOTALDISCS", []).append(total)
            continue
        for part in parts:
            tags.setdefault(name, []).append(part)
    return tags, None


def _read_ogg_tags(path: Path) -> tuple[dict[str, list[str]], str | None]:
    """读取 OGG（Vorbis / Opus）的注释标签。

    OGG 的注释块与 FLAC 是**同一种结构**（Vorbis comment），
    所以 `OggLayout.comments` 本身就是 `flacmeta.VorbisComments`，
    直接调用它已有的 `all_tags()` 即可，不必另写解析。
    """
    from . import oggmeta

    try:
        layout = oggmeta.ogg_parse_file(path)
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"
    comments = getattr(layout, "comments", None)
    if comments is None:
        return {}, None
    return comments.all_tags(), None


def read_all_tags(path: Path) -> tuple[dict[str, list[str]], str | None]:
    """读取文件的**全部**标签（不只是匹配用的白名单），供标签编辑器使用。

    返回 (标签, 错误说明)。错误说明与空字典是**两件事**：
    空字典是「这个文件确实没有标签」，错误是「我们读不了」——
    混为一谈会让界面显示成「无标签」，使用者会以为文件是空的。

    三种容器各走各的解析器：
      FLAC → Vorbis comment；OGG/Opus → 同一结构的 Vorbis comment；
      MP3  → ID3v2 文本帧。
    """
    fmt, _handle, error = detect_media_format(path)

    # **格式识别成功就照常读标签**，不因为「时长探测失败」而整体放弃。
    # 探测走的是 MPEG 帧同步，而 ID3 / Vorbis comment 的解析与它无关——
    # 一个帧头略有异常的文件不该连标签都读不出来。
    # 只有在**格式本身没认出来**时才直接返回错误。
    if error is not None and fmt in (MediaFormat.UNSUPPORTED, None):
        return {}, error

    if fmt is MediaFormat.FLAC:
        from . import flacmeta

        try:
            layout = flacmeta.flac_parse_file(path)
        except Exception as exc:  # noqa: BLE001 — 解析失败要如实上报
            return {}, f"{type(exc).__name__}: {exc}"
        comments = layout.comments()
        if comments is None:
            return {}, None
        return comments.all_tags(), None

    if fmt in (MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
        tags, err = _read_ogg_tags(path)
        return (tags, None) if err is None else (tags, err or error)

    if fmt is MediaFormat.MP3:
        tags, err = _read_id3_tags(path)
        # 标签读出来了就算成功——时长探测的报错不该盖过它
        return (tags, None) if err is None else (tags, err or error)

    return {}, f"暂不支持读取 {fmt.value} 的全部标签"


def read_audio_meta(path: Path, root: Path, *, probe_duration: bool = True) -> AudioMeta:
    """读取一个音频文件的格式、时长、标签与内嵌歌词。"""
    stat = path.stat()
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    fmt, handle, error = detect_media_format(path)

    duration: float | None = None
    if handle is not None and probe_duration:
        try:
            length = getattr(getattr(handle, "info", None), "length", None)
            duration = float(length) if length is not None else None
        except Exception:
            duration = None

    info = AudioInfo(
        path=path,
        rel_path=rel,
        extension=path.suffix.lower(),
        media_format=fmt,
        duration=duration,
        size=stat.st_size,
        mtime=stat.st_mtime,
        ctime=stat.st_ctime,
        # inode 是「同一个文件改名」的唯一可靠锚点（见 AudioInfo 的说明）
        inode=getattr(stat, "st_ino", 0) or 0,
        parse_error=error,
    )

    if error is not None or not fmt.supported:
        return AudioMeta(info=info, tags={}, embedded=EmbeddedLyricsFinding(has_cover=False))

    tags = _read_match_tags(handle, fmt)
    embedded = read_embedded_lyrics(handle, fmt, duration_seconds=duration)
    return AudioMeta(info=info, tags=tags, embedded=embedded)


#: 本项目标签名 → 可能的 ID3 帧 ID（一个名字可能对应多个帧）。
#: 由 `_ID3_TEXT_FRAMES` 反向生成，保证读写两侧用的是同一张表。
_ID3_FRAME_FOR_NAME: dict[str, tuple[str, ...]] = {}
for _frame, _name in _ID3_TEXT_FRAMES.items():
    _ID3_FRAME_FOR_NAME.setdefault(_name, ())
    _ID3_FRAME_FOR_NAME[_name] += (_frame,)
del _frame, _name


def _read_match_tags(handle: Any, fmt: MediaFormat) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if fmt is MediaFormat.MP3:
        id3 = getattr(handle, "tags", None)
        if id3 is None:
            return out
        # **必须用帧 ID 查，不能用标签名。**
        # 实际缺陷：这里原先拿 `_MATCH_TAG_KEYS`（Vorbis 风格名，如 TITLE）
        # 直接去查 mutagen 的 ID3 对象，而 ID3 的键是帧 ID（TIT2）——
        # 于是 MP3 的标签**一个都读不出来**。
        #
        # 后果不只是显示：匹配时标题与艺人为空，那首 MP3 因此永远
        # 找不到歌词（实际 `Just The Way You Are.mp3` 正是如此）。
        for key in _MATCH_TAG_KEYS:
            frames = _ID3_FRAME_FOR_NAME.get(key)
            if not frames:
                continue
            for frame in frames:
                if frame not in id3:
                    continue
                values = _id3_values(id3[frame])
                if values:
                    out[key] = values
                    break
        # MusicBrainz 的几个 ID 字段用的是专用帧，单独处理
        for key, frame in _MBID_TO_ID3.items():
            if frame in id3:
                values = _id3_values(id3[frame])
                if values:
                    out[key] = values
        return out

    # Vorbis comment：键大小写不敏感
    vc = getattr(handle, "tags", None)
    if vc is None:
        return out
    lowered = {str(k).upper(): k for k in vc.keys()}  # noqa: SIM118 — mutagen 的 VComment 迭代产出的是键值对
    for key in _MATCH_TAG_KEYS:
        real = lowered.get(key)
        if real is None:
            continue
        values = [str(v) for v in vc.get(real, [])]
        if values:
            out[key] = values
    return out


# MusicBrainz 标签在 ID3 中的帧名
_MBID_TO_ID3: dict[str, str] = {
    "MUSICBRAINZ_TRACKID": "UFID:http://musicbrainz.org",
    "MUSICBRAINZ_RELEASETRACKID": "TXXX:MusicBrainz Release Track Id",
    "MUSICBRAINZ_ALBUMID": "TXXX:MusicBrainz Album Id",
    "MUSICBRAINZ_ARTISTID": "TXXX:MusicBrainz Artist Id",
    "MUSICBRAINZ_RELEASEGROUPID": "TXXX:MusicBrainz Release Group Id",
}


def _id3_values(frame: Any) -> list[str]:
    text = getattr(frame, "text", None)
    if text is None:
        data = getattr(frame, "data", None)
        if not data:
            return []
        if isinstance(data, (bytes, bytearray)):
            return [bytes(data).decode("utf-8", errors="replace").strip("\x00")]
        return [str(data)]
    if isinstance(text, (list, tuple)):
        return [str(t) for t in text]
    return [str(text)]


# ---------------------------------------------------------------------------
# 内嵌歌词
# ---------------------------------------------------------------------------


def read_embedded_lyrics(
    handle: Any,
    fmt: MediaFormat,
    *,
    duration_seconds: float | None = None,
) -> EmbeddedLyricsFinding:
    """读取内嵌歌词并按时间轴分类。

    会读取**所有**歌词值/帧，不只读第一个。
    """
    if fmt is MediaFormat.MP3:
        return _read_id3_lyrics(handle, duration_seconds=duration_seconds)
    return _read_vorbis_lyrics(handle, duration_seconds=duration_seconds)


def _read_vorbis_lyrics(handle: Any, *, duration_seconds: float | None) -> EmbeddedLyricsFinding:
    vc = getattr(handle, "tags", None)
    has_cover = _vorbis_has_cover(handle, vc)
    if vc is None:
        return EmbeddedLyricsFinding(
            state=LyricsState.ABSENT, origin=LyricsOrigin.NONE, has_cover=has_cover
        )

    matched_keys: list[str] = []
    texts: list[str] = []
    for key in vc.keys():  # noqa: SIM118 — mutagen 的 VComment 迭代产出的是键值对
        if str(key).upper() in VORBIS_LYRICS_FIELDS:
            matched_keys.append(str(key))
            for value in vc.get(key, []):
                if value:
                    texts.append(str(value))

    if not matched_keys:
        return EmbeddedLyricsFinding(
            state=LyricsState.ABSENT, origin=LyricsOrigin.NONE, has_cover=has_cover
        )

    return _classify_texts(
        texts,
        origin=LyricsOrigin.VORBIS_COMMENT,
        fields=tuple(matched_keys),
        has_cover=has_cover,
        duration_seconds=duration_seconds,
    )


def read_embedded_lyrics_texts(path: Path) -> tuple[list[str], str | None]:
    """取出文件里内嵌的歌词**原文**（可能有多个字段/帧，全部返回）。

    与 `read_embedded_lyrics` 的分工：那边只给分类结论，不给正文。
    需要正文的场景只有一个——把内嵌歌词落成同名 `.lrc`（库里大量文件
    仅有内嵌歌词，而播放器只认 `.lrc`）。此时内容必须逐字来自文件，
    不能拿分类结果反推。

    返回 (正文列表, 错误说明)。读不出来是**错误**，不是「没有歌词」。
    """
    fmt, handle, error = detect_media_format(path)
    if fmt is MediaFormat.MP3:
        id3 = getattr(handle, "tags", None)
        if id3 is None:
            return [], error
        texts: list[str] = []
        for frame in id3.getall("USLT"):
            if isinstance(frame, USLT) and frame.text:
                texts.append(str(frame.text))
        for frame in id3.getall("SYLT"):
            if isinstance(frame, SYLT):
                converted = _sylt_to_lrc(frame)
                if converted:
                    texts.append(converted)
        return texts, error

    if fmt in (MediaFormat.FLAC, MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
        tags, err = read_all_tags(path)
        if err is not None:
            return [], err
        texts = []
        for key, values in tags.items():
            if key.upper() in VORBIS_LYRICS_FIELDS:
                texts.extend(v for v in values if v.strip())
        return texts, None

    return [], error or f"暂不支持从 {getattr(fmt, 'value', fmt)} 读取歌词正文"


def _read_id3_lyrics(handle: Any, *, duration_seconds: float | None) -> EmbeddedLyricsFinding:
    id3: ID3 | None = getattr(handle, "tags", None)
    has_cover = bool(id3 is not None and any(k.startswith("APIC") for k in id3))
    if id3 is None:
        return EmbeddedLyricsFinding(
            state=LyricsState.ABSENT, origin=LyricsOrigin.NONE, has_cover=has_cover
        )

    texts: list[str] = []
    fields: list[str] = []
    problems: list[str] = []

    for frame in id3.getall("USLT"):
        if not isinstance(frame, USLT):
            continue
        fields.append(f"USLT:{frame.lang or 'XXX'}")
        if frame.text:
            texts.append(str(frame.text))

    # SYLT：时间单位可能是毫秒(format=2)或 MPEG 帧(format=1)
    sylt_frames = [f for f in id3.getall("SYLT") if isinstance(f, SYLT)]
    for frame in sylt_frames:
        fields.append(f"SYLT:{frame.lang or 'XXX'}")
        unit = getattr(frame, "format", None)
        if unit == 1:
            # MPEG 帧计时，无法在不引入帧率假设的前提下可靠转换 → 明确拒绝转换
            problems.append("sylt_mpeg_frame_timing_unsupported")
            continue
        converted = _sylt_to_lrc(frame)
        if converted:
            texts.append(converted)
        else:
            problems.append("sylt_empty")

    if not fields:
        return EmbeddedLyricsFinding(
            state=LyricsState.ABSENT, origin=LyricsOrigin.NONE, has_cover=has_cover
        )

    result = _classify_texts(
        texts,
        origin=LyricsOrigin.ID3_SYLT if sylt_frames else LyricsOrigin.ID3_USLT,
        fields=tuple(fields),
        has_cover=has_cover,
        duration_seconds=duration_seconds,
    )
    if not problems:
        return result

    # 把帧级问题并入结果，供报告使用
    if result.parsed is not None:
        merged = tuple(dict.fromkeys(tuple(result.parsed.problems) + tuple(problems)))
        result = replace(result, parsed=replace(result.parsed, problems=merged))
    elif result.state is LyricsState.ABSENT:
        # 只有无法可靠转换的 SYLT，没有可用歌词文本：
        # 明确标为不可解析，而不是「没有歌词」
        result = replace(result, state=LyricsState.UNPARSEABLE)
    return result


def _vorbis_has_cover(handle: Any, vc: Any) -> bool:
    """Vorbis 系的封面可能在 FLAC PICTURE 块，也可能在 METADATA_BLOCK_PICTURE。"""
    if getattr(handle, "pictures", None):
        return True
    if vc is None:
        return False
    keys = {str(k).upper() for k in vc.keys()}  # noqa: SIM118 — 同上
    return bool(keys & {"METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME"})


def _sylt_to_lrc(frame: SYLT) -> str:
    """把 SYLT 转为 LRC 文本。仅支持毫秒计时（format == 2）。"""
    content = getattr(frame, "text", None)
    if not content:
        return ""
    lines: list[str] = []
    for item in content:
        try:
            text, ms = item
        except (TypeError, ValueError):
            continue
        if text is None or ms is None:
            continue
        minutes, rem = divmod(int(ms), 60_000)
        seconds, millis = divmod(rem, 1_000)
        lines.append(f"[{minutes:02d}:{seconds:02d}.{millis // 10:02d}]{text}")
    return "\n".join(lines)


def _classify_texts(
    texts: list[str],
    *,
    origin: LyricsOrigin,
    fields: tuple[str, ...],
    has_cover: bool,
    duration_seconds: float | None,
) -> EmbeddedLyricsFinding:
    """把多个歌词文本合并分类。

    取时间轴质量最高者作为有效状态：synced > plain_text > absent。
    """
    if not texts:
        return EmbeddedLyricsFinding(
            state=LyricsState.ABSENT, origin=origin, fields=fields, has_cover=has_cover
        )

    parsed_list = [
        parse_lrc(t, source_label=f"{origin.value}:{fields}", duration_seconds=duration_seconds)
        for t in texts
    ]

    rank = {
        LyricsState.SYNCED: 4,
        LyricsState.SYNCED_SUSPICIOUS: 3,
        LyricsState.PLAIN_TEXT: 2,
        LyricsState.UNPARSEABLE: 1,
        LyricsState.ABSENT: 0,
    }
    best = max(parsed_list, key=lambda p: rank[p.state])

    # 合并所有问题，便于报告
    merged: list[str] = []
    for p in parsed_list:
        merged.extend(p.problems)
    if merged:
        best = replace(best, problems=tuple(dict.fromkeys(merged)))

    return EmbeddedLyricsFinding(
        state=best.state, origin=origin, fields=fields, parsed=best, has_cover=has_cover
    )
