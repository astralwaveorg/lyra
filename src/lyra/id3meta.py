"""MP3 的 ID3v2 元数据字节级受限写入。

**为什么不用 `mutagen` 的 `ID3.save()`**：

mutagen 的 `ID3` 默认**会做版本转换**（例如把 v2.3 的帧改写成 v2.4 语义，
或反过来），还会整体重新序列化所有帧。结果就是：写完一次歌词，用户文件里
**每一帧**的字节都可能变了，无法证明「只动了歌词」。规范明确禁止这种兜底。

因此本模块直接操作 ID3v2 的字节结构：

    "ID3" + 主版本(1) + 修订(1) + 标志(1) + 长度(4, synchsafe)
    [ 帧 ... ] [ 填充 ... ]
    + MPEG 音频数据
    + 尾部标签（ID3v1 / APEv2，原样保留）

- **主版本绝不改动**：v2.3 进去 v2.3 出来。允许改版本就等于悄悄替用户做了
  一个他没批准的变更，而且 v2.3 / v2.4 的帧长度字段语义不同（v2.3 是普通
  大端 32 位，v2.4 是 synchsafe），混用会直接写坏文件。
- **只处理白名单帧**（`formats.ID3_LYRICS_FRAMES` = USLT / SYLT）。
  `TEXT`（作词人）、`TCOM`（作曲）**绝不在白名单内**，必须原样保留。
- **非歌词帧按原始字节照抄**（含 APIC 封面、COMM、UFID、TXXX、未知帧）。

## 明确拒绝的结构

以下情况无法在「保持其余数据不变」的前提下安全更新，因此**抛异常拒绝**，
而不是通用重写：

- ID3v2 全局 unsynchronisation（标志位 0x80）
- 含扩展头（标志位 0x40）——扩展头可能带 CRC，CRC 依赖整个标签的字节布局
- 任一帧被压缩或加密
- 不支持的 ID3v2 主版本（只支持 2.3 / 2.4）
- 文件里根本没有 ID3v2 标签（没有「原有主版本」可保持）

## 音频哈希

`id3_audio_hash` 对**MPEG 音频数据段**求 SHA-256：跳过头部 ID3v2，跳过尾部
ID3v1（末尾 128 字节）与 APEv2。这样改写标签不会影响音频哈希。

有意**不**使用 `mutagen.mp3.MP3.info` 之类的解析结果：那些依赖解析器对
MPEG 帧的容错，文件局部损坏时可能给出错误信息。这里只做「按已知边界切段 +
哈希」，不解释音频内容。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .formats import ID3_LYRICS_FRAMES

__all__ = [
    "Id3FormatError",
    "Id3Frame",
    "Id3Layout",
    "compare_non_lyrics",
    "id3_audio_hash",
    "id3_parse_bytes",
    "id3_parse_file",
    "id3_rewrite_cover",
    "id3_rewrite_tags",
    "id3_rewrite_with_lyrics",
    "parse_apic",
    "parse_txxx",
    "TXXX_DESCRIPTIONS",
    "TXXX_KEYS_BY_DESC",
]

ID3_MAGIC = b"ID3"
ID3V1_SIZE = 128
ID3V1_MAGIC = b"TAG"
APE_FOOTER_SIZE = 32
APE_MAGIC = b"APETAGEX"

#: 新建或增长标签时预留的填充字节，便于日后原地再编辑
NEW_TAG_PADDING = 1024

#: 常规标签键 → ID3 文本帧。多值 / 非标准字段走 TXXX。
NATIVE_TAG_FRAMES: dict[str, str] = {
    "TITLE": "TIT2",
    "ARTIST": "TPE1",
    "ALBUMARTIST": "TPE2",
    "ALBUM": "TALB",
    "GENRE": "TCON",
    "COMPOSER": "TCOM",
    "LYRICIST": "TEXT",
    "LABEL": "TPUB",
    "COPYRIGHT": "TCOP",
    "ISRC": "TSRC",
}

#: 非标准字段 → TXXX 描述；尽量与 Picard 的写法一致，便于互操作。
TXXX_DESCRIPTIONS: dict[str, str] = {
    "ARTISTS": "Artists",
    "ALBUMARTISTS": "Album Artists",
    "ARTISTSORT": "Artists Sort Order",
    "ALBUMARTISTSORT": "Album Artists Sort Order",
    "MUSICBRAINZ_TRACKID": "MusicBrainz Track Id",
    "MUSICBRAINZ_ALBUMID": "MusicBrainz Album Id",
    "MUSICBRAINZ_ARTISTID": "MusicBrainz Artist Id",
    "MUSICBRAINZ_ALBUMARTISTID": "MusicBrainz Album Artist Id",
    "MUSICBRAINZ_RELEASEGROUPID": "MusicBrainz Release Group Id",
    "MUSICBRAINZ_RELEASETRACKID": "MusicBrainz Release Track Id",
    "ACOUSTID_ID": "Acoustid Id",
    "CATALOGNUMBER": "CATALOGNUMBER",
    "BARCODE": "BARCODE",
    "ASIN": "ASIN",
    "RELEASECOUNTRY": "RELEASECOUNTRY",
    "RELEASESTATUS": "RELEASESTATUS",
    "RELEASETYPE": "RELEASETYPE",
    "MEDIA": "MEDIA",
    "SCRIPT": "SCRIPT",
    "ORIGINALDATE": "ORIGINALDATE",
    "ORIGINALYEAR": "ORIGINALYEAR",
}

TXXX_KEYS_BY_DESC = {
    desc.casefold(): key for key, desc in TXXX_DESCRIPTIONS.items()
}

#: 多值 TXXX 用分号分隔；MusicBrainz ID 还允许斜杠。
_ARTIST_LIST_KEYS = frozenset({
    "ARTISTS", "ALBUMARTISTS", "ARTISTSORT", "ALBUMARTISTSORT",
    "RELEASETYPE",
})
_MBID_LIST_KEYS = frozenset({
    "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID",
    "MUSICBRAINZ_RELEASEGROUPID", "MUSICBRAINZ_RELEASETRACKID",
    "ACOUSTID_ID",
})

# 文本编码字节
_ENC_LATIN1 = 0x00
_ENC_UTF16 = 0x01  # 带 BOM
_ENC_UTF16_BE = 0x02
_ENC_UTF8 = 0x03

# 标签级标志
_FLAG_UNSYNCHRONISATION = 0x80
_FLAG_EXTENDED_HEADER = 0x40

# 帧级标志（第二个标志字节）
_V3_FRAME_COMPRESSED = 0x80
_V3_FRAME_ENCRYPTED = 0x40
_V4_FRAME_COMPRESSED = 0x08
_V4_FRAME_ENCRYPTED = 0x04

_SUPPORTED_VERSIONS = (3, 4)

_COPY_CHUNK = 1 << 20


class Id3FormatError(Exception):
    """结构不符合可安全更新的范围，或含本实现不支持的结构。"""


# ---------------------------------------------------------------------------
# synchsafe 整数
# ---------------------------------------------------------------------------


def _from_synchsafe(data: bytes, label: str = "synchsafe") -> int:
    if len(data) != 4:
        raise Id3FormatError(f"{label}: 需要 4 字节，实际 {len(data)}")
    if any(byte & 0x80 for byte in data):
        # 最高位必须为 0；否则说明这不是 synchsafe 编码（例如把普通 32 位当成了它）
        raise Id3FormatError(f"{label}: 字节最高位不为 0，不是合法的 synchsafe 整数")
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _to_synchsafe(value: int) -> bytes:
    if value < 0 or value > 0x0FFFFFFF:
        raise Id3FormatError(f"synchsafe 取值范围溢出: {value}")
    return bytes(
        ((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F)
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Id3Frame:
    """一帧，`raw` 是**含 10 字节帧头的完整原始字节**。

    保留完整原始字节而不是解析后的字段，是为了「非歌词帧逐字节照抄」——
    解析再序列化必然引入规范化差异，那正是本模块要避免的。
    """

    frame_id: str
    raw: bytes

    @property
    def body(self) -> bytes:
        return self.raw[10:]


@dataclass(slots=True)
class Id3Layout:
    """ID3v2 标签 + 音频区间 + 尾部标签的布局视图。"""

    version: int
    revision: int
    flags: int
    frames: tuple[Id3Frame, ...]
    padding_size: int
    tag_size: int
    """ID3v2 标签总长，含 10 字节头。"""
    audio_offset: int
    audio_end: int
    audio_sha256: str
    trailer: bytes
    file_size: int

    def lyrics_frames(self, frame_ids: Iterable[str] = ID3_LYRICS_FRAMES) -> list[Id3Frame]:
        wanted = {frame_id.upper() for frame_id in frame_ids}
        return [f for f in self.frames if f.frame_id.upper() in wanted]

    def audio_hash(self) -> str:
        return self.audio_sha256


# ---------------------------------------------------------------------------
# 帧标志
# ---------------------------------------------------------------------------


def _reject_unsupported_frame_flags(
    flags: bytes, version: int, frame_id: str, label: str
) -> None:
    """压缩/加密帧的内容不可解释，无法保证「保持其余数据不变」。"""
    if len(flags) != 2:
        raise Id3FormatError(f"{label}: 帧 {frame_id!r} 标志长度异常")
    status, format_flags = flags[0], flags[1]
    if version == 3:
        if format_flags & _V3_FRAME_COMPRESSED:
            raise Id3FormatError(f"{label}: 帧 {frame_id!r} 被压缩，拒绝提交")
        if format_flags & _V3_FRAME_ENCRYPTED:
            raise Id3FormatError(f"{label}: 帧 {frame_id!r} 被加密，拒绝提交")
    else:
        if format_flags & _V4_FRAME_COMPRESSED:
            raise Id3FormatError(f"{label}: 帧 {frame_id!r} 被压缩，拒绝提交")
        if format_flags & _V4_FRAME_ENCRYPTED:
            raise Id3FormatError(f"{label}: 帧 {frame_id!r} 被加密，拒绝提交")
    # 状态标志（标签/文件保持位、只读位）不影响字节级改写，允许存在
    _ = status


def _is_valid_frame_id(raw: bytes) -> bool:
    """帧标识必须是 4 个 ASCII 大写字母或数字。"""
    return len(raw) == 4 and all(
        0x41 <= byte <= 0x5A or 0x30 <= byte <= 0x39 for byte in raw
    )


# ---------------------------------------------------------------------------
# 尾部标签定位
# ---------------------------------------------------------------------------


def _trailer_start(data: bytes, label: str) -> int:
    """返回音频数据的结束偏移，即尾部标签（ID3v1 / APEv2）的起点。

    标准排布是 `[音频][APEv2][ID3v1]`，所以先看 ID3v1 再看 APEv2。
    """
    end = len(data)

    if end >= ID3V1_SIZE and data[end - ID3V1_SIZE : end - ID3V1_SIZE + 3] == ID3V1_MAGIC:
        end -= ID3V1_SIZE

    if end >= APE_FOOTER_SIZE and data[end - APE_FOOTER_SIZE : end - APE_FOOTER_SIZE + 8] == APE_MAGIC:
        size = int.from_bytes(data[end - APE_FOOTER_SIZE + 12 : end - APE_FOOTER_SIZE + 16], "little")
        if size < APE_FOOTER_SIZE:
            raise Id3FormatError(f"{label}: APEv2 标签长度异常 {size}")
        start = end - size
        if start < 0:
            raise Id3FormatError(f"{label}: APEv2 标签长度超出文件")
        # APEv2 可以同时有头与尾，此时还要再往前 32 字节
        if start >= APE_FOOTER_SIZE and data[start - APE_FOOTER_SIZE : start - APE_FOOTER_SIZE + 8] == APE_MAGIC:
            start -= APE_FOOTER_SIZE
        end = start

    return end


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_frames(
    body: bytes, version: int, label: str
) -> tuple[tuple[Id3Frame, ...], int]:
    frames: list[Id3Frame] = []
    offset = 0
    limit = len(body)

    while offset + 10 <= limit:
        frame_id = body[offset : offset + 4]
        if frame_id[0] == 0:
            # 遇到填充区，后面应当全是 0
            break
        if not _is_valid_frame_id(frame_id):
            raise Id3FormatError(f"{label}: 非法帧标识 {frame_id!r} @ {offset}")

        raw_size = body[offset + 4 : offset + 8]
        if version == 4:
            frame_size = _from_synchsafe(raw_size, f"{label}: 帧 {frame_id.decode('ascii')} 长度")
        else:
            frame_size = int.from_bytes(raw_size, "big")

        flags = body[offset + 8 : offset + 10]
        _reject_unsupported_frame_flags(flags, version, frame_id.decode("ascii"), label)

        total = 10 + frame_size
        if offset + total > limit:
            raise Id3FormatError(
                f"{label}: 帧 {frame_id.decode('ascii')!r} 声明 {frame_size} 字节，超出标签范围"
            )
        frames.append(
            Id3Frame(frame_id.decode("ascii"), bytes(body[offset : offset + total]))
        )
        offset += total

    return tuple(frames), limit - offset


def id3_parse_file(path: Path) -> Id3Layout:
    return _parse(path.read_bytes(), str(path))


def id3_parse_bytes(data: bytes, label: str = "<bytes>") -> Id3Layout:
    return _parse(data, label)


def _parse(data: bytes, label: str) -> Id3Layout:
    if data[:3] != ID3_MAGIC:
        raise Id3FormatError(
            f"{label}: 没有 ID3v2 标签。没有「原有主版本」可保持，"
            "拒绝自行选择版本创建（避免替用户做未批准的变更）"
        )

    version = data[3]
    revision = data[4]
    flags = data[5]
    if version not in _SUPPORTED_VERSIONS:
        raise Id3FormatError(f"{label}: 不支持 ID3v2.{version}（仅支持 2.3 / 2.4）")
    if flags & _FLAG_UNSYNCHRONISATION:
        raise Id3FormatError(
            f"{label}: ID3v2 全局 unsynchronisation 已启用；标签字节与实际内容不同，拒绝提交"
        )
    if flags & _FLAG_EXTENDED_HEADER:
        raise Id3FormatError(
            f"{label}: ID3v2 含扩展头（可能带 CRC / 限制标志），依赖整个标签的字节布局；拒绝提交"
        )

    tag_size = 10 + _from_synchsafe(data[6:10], f"{label}: 标签长度")
    if tag_size > len(data):
        raise Id3FormatError(f"{label}: ID3v2 标签长度 {tag_size} 超出文件大小 {len(data)}")
    if data[tag_size : tag_size + 3] == ID3_MAGIC:
        raise Id3FormatError(f"{label}: 连续出现多个 ID3v2 标签，本实现不支持")

    frames, padding = _parse_frames(data[10:tag_size], version, label)

    audio_end = _trailer_start(data, label)
    if audio_end < tag_size:
        raise Id3FormatError(f"{label}: 尾部标签与 ID3v2 标签重叠")

    return Id3Layout(
        version=version,
        revision=revision,
        flags=flags,
        frames=frames,
        padding_size=padding,
        tag_size=tag_size,
        audio_offset=tag_size,
        audio_end=audio_end,
        audio_sha256=hashlib.sha256(data[tag_size:audio_end]).hexdigest(),
        trailer=bytes(data[audio_end:]),
        file_size=len(data),
    )


def id3_audio_hash(path: Path) -> str:
    """MPEG 音频数据段的 SHA-256（跳过 ID3v2 头与 ID3v1/APEv2 尾部）。"""
    try:
        return id3_parse_file(path).audio_hash()
    except Id3FormatError as exc:
        if "没有 ID3v2 标签" not in str(exc):
            raise
        return _layout_without_tag(path).audio_hash()


# ---------------------------------------------------------------------------
# 文本编码
# ---------------------------------------------------------------------------


def _encode_text(text: str, encoding: int) -> bytes:
    if encoding == _ENC_LATIN1:
        return text.encode("latin-1", "replace")
    if encoding == _ENC_UTF8:
        return text.encode("utf-8")
    if encoding == _ENC_UTF16_BE:
        return text.encode("utf-16-be")
    return text.encode("utf-16")  # 带 BOM


def _encode_terminated(text: str, encoding: int) -> bytes:
    terminator = b"\x00" if encoding in (_ENC_LATIN1, _ENC_UTF8) else b"\x00\x00"
    return _encode_text(text, encoding) + terminator


def _decode_terminated(data: bytes, offset: int, encoding: int) -> tuple[str, int]:
    if encoding in (_ENC_LATIN1, _ENC_UTF8):
        end = data.find(b"\x00", offset)
        if end < 0:
            end = len(data)
        codec = "latin-1" if encoding == _ENC_LATIN1 else "utf-8"
        return data[offset:end].decode(codec, "replace"), min(end + 1, len(data))

    position = offset
    while position + 1 < len(data):
        if data[position] == 0 and data[position + 1] == 0:
            break
        position += 2
    raw = data[offset:position]
    codec = "utf-16-be" if encoding == _ENC_UTF16_BE else "utf-16"
    if encoding == _ENC_UTF16 and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # 声称带 BOM 却没有；按本机字节序解码，避免直接抛错
        codec = "utf-16-le"
    return raw.decode(codec, "replace"), min(position + 2, len(data))


def _frame_encoding_for_version(version: int) -> int:
    """按 ID3 主版本选择歌词文本编码。

    - v2.4 允许 UTF-8（0x03），用它是标准做法。
    - **v2.3 规范里没有 UTF-8**（只允许 ISO-8859-1 与带 BOM 的 UTF-16）。
      中文歌词用 ISO-8859-1 会丢失，所以 v2.3 必须用 UTF-16 + BOM。
      在 v2.3 标签里写 0x03 是超规范用法，严格播放器会读成乱码。
    """
    return _ENC_UTF8 if version == 4 else _ENC_UTF16


def _parse_uslt(body: bytes) -> tuple[int, str, str, str] | None:
    """解析 USLT 帧体，返回 (编码, 语言, 描述符, 歌词)。"""
    if len(body) < 4:
        return None
    encoding = body[0]
    language = body[1:4].decode("ascii", "replace")
    descriptor, position = _decode_terminated(body, 4, encoding)
    if encoding in (_ENC_LATIN1, _ENC_UTF8):
        text = body[position:].decode(
            "latin-1" if encoding == _ENC_LATIN1 else "utf-8", "replace"
        )
    else:
        codec = "utf-16-be" if encoding == _ENC_UTF16_BE else "utf-16"
        raw = body[position:]
        if encoding == _ENC_UTF16 and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            codec = "utf-16-le"
        text = raw.decode(codec, "replace")
    return encoding, language, descriptor, text


def _build_frame(frame_id: str, body: bytes, version: int) -> bytes:
    # v2.3 的帧长度是普通大端 32 位，v2.4 是 synchsafe —— 混用会直接写坏文件
    size = _to_synchsafe(len(body)) if version == 4 else len(body).to_bytes(4, "big")
    return frame_id.encode("ascii") + size + b"\x00\x00" + body


def _build_lyrics_frame(
    layout: Id3Layout,
    lyrics: str,
    *,
    language: str | None,
    descriptor: str | None,
) -> Id3Frame:
    """构造一个 USLT 帧。

    沿用原有 USLT 的**语言代码与描述符**：不少播放器/词库以 (语言, 描述符)
    作为歌词条目的键，无端改写会让用户已经建立的关联失效。
    """
    encoding = _frame_encoding_for_version(layout.version)
    existing_language: str | None = None
    existing_descriptor: str | None = None
    for frame in layout.frames:
        if frame.frame_id.upper() != "USLT":
            continue
        parsed = _parse_uslt(frame.body)
        if parsed is not None:
            existing_language = parsed[1]
            existing_descriptor = parsed[2]
        break

    final_language = language or existing_language or "XXX"
    final_descriptor = descriptor if descriptor is not None else (existing_descriptor or "")

    body = bytearray()
    body.append(encoding)
    body += final_language.encode("ascii", "replace")[:3].ljust(3, b" ")
    body += _encode_terminated(final_descriptor, encoding)
    body += _encode_text(lyrics, encoding)

    return Id3Frame("USLT", _build_frame("USLT", bytes(body), layout.version))


# ---------------------------------------------------------------------------
# 白名单校验
# ---------------------------------------------------------------------------


def _validated_clear_fields(clear_fields: Iterable[str]) -> frozenset[str]:
    """只允许清理白名单内的歌词帧。

    这道校验是有意的防御：若有人传入 `("TEXT",)`，`TEXT`（作词人）会被清空，
    而这属于不可恢复的用户数据损失。宁可让调用方报错。
    """
    allowed = {frame_id.upper() for frame_id in ID3_LYRICS_FRAMES}
    requested = {frame_id.upper() for frame_id in clear_fields}
    unknown = requested - allowed
    if unknown:
        raise ValueError(
            f"{sorted(unknown)} 不在 ID3 歌词帧白名单 {sorted(allowed)} 内。"
            "TEXT 是作词人，不是歌词，禁止清理。"
        )
    return requested


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _decode_text_rest(raw: bytes, encoding: int) -> str:
    if not raw:
        return ""
    if encoding == _ENC_LATIN1:
        text = raw.decode("latin-1", "replace")
    elif encoding == _ENC_UTF8:
        text = raw.decode("utf-8", "replace")
    elif encoding == _ENC_UTF16_BE:
        text = raw.decode("utf-16-be", "replace")
    else:
        codec = "utf-16"
        if not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            codec = "utf-16-le"
        text = raw.decode(codec, "replace")
    return text.rstrip("\x00")


def parse_txxx(body: bytes) -> tuple[str, str] | None:
    """解析 TXXX 帧体，返回 (描述, 值)。"""
    if not body:
        return None
    encoding = body[0]
    description, position = _decode_terminated(body, 1, encoding)
    return description, _decode_text_rest(body[position:], encoding)


def split_txxx_values(key: str, value: str) -> list[str]:
    """把多值 TXXX 拆成列表；艺人用分号，ID 还允许斜杠。"""
    if key in _ARTIST_LIST_KEYS:
        parts = re.split(r"[\x00;]", value)
    elif key in _MBID_LIST_KEYS:
        parts = re.split(r"[\x00;/]", value)
    else:
        parts = value.split("\x00")
    return [part.strip() for part in parts if part.strip()]


def _text_frame(frame_id: str, text: str, version: int) -> bytes:
    encoding = _frame_encoding_for_version(version)
    return _build_frame(
        frame_id, bytes((encoding,)) + _encode_text(text, encoding), version
    )


def _txxx_frame(description: str, text: str, version: int) -> bytes:
    encoding = _frame_encoding_for_version(version)
    body = (
        bytes((encoding,))
        + _encode_terminated(description, encoding)
        + _encode_text(text, encoding)
    )
    return _build_frame("TXXX", body, version)


def _existing_text(layout: Id3Layout, frame_id: str) -> str:
    for frame in layout.frames:
        if frame.frame_id != frame_id or not frame.body:
            continue
        return _decode_text_rest(frame.body[1:], frame.body[0])
    return ""


def _layout_without_tag(path: Path) -> Id3Layout:
    data = path.read_bytes()
    end = _trailer_start(data, str(path))
    return Id3Layout(
        version=4,
        revision=0,
        flags=0,
        frames=(),
        padding_size=0,
        tag_size=0,
        audio_offset=0,
        audio_end=end,
        audio_sha256=hashlib.sha256(data[:end]).hexdigest(),
        trailer=bytes(data[end:]),
        file_size=len(data),
    )


def id3_rewrite_tags(
    source: Path,
    target: Path,
    *,
    tags: Mapping[str, Sequence[str]],
    remove: Iterable[str] = (),
) -> None:
    """写入普通标签帧，保持音频段、封面、歌词和其它帧逐字节不变。

    与歌词写入共用同一套 ID3 布局与填充策略：保持原主版本，只替换目标帧，
    非目标帧（APIC/USLT/COMM/未知帧）原样照抄。文件没有 ID3v2 时新建 v2.4。
    """
    try:
        layout = id3_parse_file(source)
    except Id3FormatError as exc:
        if "没有 ID3v2 标签" not in str(exc):
            raise
        layout = _layout_without_tag(source)

    normalized = {
        str(key).upper(): [str(v) for v in values if str(v).strip()]
        for key, values in tags.items()
    }
    remove_set = {str(key).upper() for key in remove}

    drop_ids: set[str] = set()
    drop_descs: set[str] = set()
    new_frames: list[bytes] = []

    def first(key: str) -> str:
        values = normalized.get(key) or []
        return values[0].strip() if values else ""

    # 日期：不保留旧 TYER/TDAT/TIME，统一写 TDRC。
    if "DATE" in normalized or "DATE" in remove_set:
        drop_ids.update({"TDRC", "TYER", "TDAT", "TIME"})
        date = first("DATE")
        if date:
            new_frames.append(_text_frame("TDRC", date, layout.version))

    # 常规文本帧。
    for key, frame_id in NATIVE_TAG_FRAMES.items():
        if key in normalized:
            drop_ids.add(frame_id)
            values = normalized[key]
            if key in ("ARTIST", "ALBUMARTIST") and len(values) == 1:
                text = values[0]
            else:
                text = " / ".join(values)
            new_frames.append(_text_frame(frame_id, text, layout.version))
        elif key in remove_set:
            drop_ids.add(frame_id)

    # 曲序/碟序：TRCK/TPOS 的 total 与编号分开存。
    existing_track = _existing_text(layout, "TRCK")
    existing_track_num, _, existing_track_total = existing_track.partition("/")
    existing_disc = _existing_text(layout, "TPOS")
    existing_disc_num, _, existing_disc_total = existing_disc.partition("/")

    track_keys = {"TRACKNUMBER", "TRACKTOTAL", "TOTALTRACKS"}
    disc_keys = {"DISCNUMBER", "DISCTOTAL", "TOTALDISCS"}
    if normalized.keys() & track_keys or remove_set & track_keys:
        drop_ids.add("TRCK")
        number = first("TRACKNUMBER") or existing_track_num
        total = (
            first("TRACKTOTAL") or first("TOTALTRACKS") or existing_track_total
        )
        if "TRACKNUMBER" in remove_set:
            number = ""
        if remove_set & {"TRACKTOTAL", "TOTALTRACKS"}:
            total = ""
        if number:
            new_frames.append(
                _text_frame(
                    "TRCK", f"{number}/{total}" if total else number, layout.version
                )
            )
    if normalized.keys() & disc_keys or remove_set & disc_keys:
        drop_ids.add("TPOS")
        number = first("DISCNUMBER") or existing_disc_num
        total = first("DISCTOTAL") or first("TOTALDISCS") or existing_disc_total
        if "DISCNUMBER" in remove_set:
            number = ""
        if remove_set & {"DISCTOTAL", "TOTALDISCS"}:
            total = ""
        if number:
            new_frames.append(
                _text_frame(
                    "TPOS", f"{number}/{total}" if total else number, layout.version
                )
            )

    # 非标准字段走 TXXX。
    for key, description in TXXX_DESCRIPTIONS.items():
        if key in normalized:
            drop_descs.add(description.casefold())
            if key == "ARTISTSORT":
                drop_ids.add("TSOP")
            elif key == "ALBUMARTISTSORT":
                drop_ids.add("TSO2")
            values = normalized[key]
            text = "; ".join(values)
            new_frames.append(_txxx_frame(description, text, layout.version))
        elif key in remove_set:
            drop_descs.add(description.casefold())

    def drop_frame(frame: Id3Frame) -> bool:
        if frame.frame_id in drop_ids:
            return True
        if frame.frame_id == "TXXX":
            parsed = parse_txxx(frame.body)
            if parsed is not None and parsed[0].casefold() in drop_descs:
                return True
        return False

    kept = [frame.raw for frame in layout.frames if not drop_frame(frame)]
    frames_bytes = b"".join(kept) + b"".join(new_frames)

    original_body_size = layout.tag_size - 10 if layout.tag_size else 0
    if original_body_size and len(frames_bytes) <= original_body_size:
        padding = original_body_size - len(frames_bytes)
    else:
        padding = NEW_TAG_PADDING

    body = frames_bytes + b"\x00" * padding
    header = (
        ID3_MAGIC
        + bytes((layout.version, layout.revision, layout.flags))
        + _to_synchsafe(len(body))
    )

    audio_length = layout.audio_end - layout.audio_offset
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(header)
        dst.write(body)
        src.seek(layout.audio_offset)
        remaining = audio_length
        while remaining > 0:
            chunk = src.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise Id3FormatError("复制音频数据时源文件提前结束")
            dst.write(chunk)
            remaining -= len(chunk)
        dst.write(layout.trailer)
        dst.flush()
        os.fsync(dst.fileno())


def id3_rewrite_with_lyrics(
    source: Path,
    target: Path,
    *,
    lyrics: str | None,
    clear_fields: Iterable[str] = ID3_LYRICS_FRAMES,
    language: str | None = None,
    descriptor: str | None = None,
) -> None:
    """把歌词写入 MP3 的 ID3v2 标签，其余部分逐字节保留。

    `lyrics=None` 表示**只清理**白名单内的歌词帧，不写入新歌词。

    保持原有 ID3v2 主版本；标签长度优先保持与原标签一致（通过调整填充区），
    装不下时才增长并预留 `NEW_TAG_PADDING` 字节填充。

    target 必须与 source 在**同一文件系统**上，以便之后原子替换。
    """
    drop = _validated_clear_fields(clear_fields)
    layout = id3_parse_file(source)

    kept = [frame for frame in layout.frames if frame.frame_id.upper() not in drop]
    if lyrics is not None:
        kept.append(
            _build_lyrics_frame(
                layout, lyrics, language=language, descriptor=descriptor
            )
        )

    frames_bytes = b"".join(frame.raw for frame in kept)
    original_body_size = layout.tag_size - 10
    if len(frames_bytes) <= original_body_size:
        # 保持标签总长不变：文件布局稳定，占用空间不增长
        padding = original_body_size - len(frames_bytes)
    else:
        padding = NEW_TAG_PADDING

    body = frames_bytes + b"\x00" * padding
    header = (
        ID3_MAGIC
        + bytes((layout.version, layout.revision, layout.flags))
        + _to_synchsafe(len(body))
    )

    audio_length = layout.audio_end - layout.audio_offset
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(header)
        dst.write(body)
        src.seek(layout.audio_offset)
        remaining = audio_length
        while remaining > 0:
            chunk = src.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise Id3FormatError("复制音频数据时源文件提前结束")
            dst.write(chunk)
            remaining -= len(chunk)
        # 尾部 ID3v1 / APEv2 原样保留
        dst.write(layout.trailer)
        dst.flush()
        os.fsync(dst.fileno())


# ---------------------------------------------------------------------------
# 可验证证明
# ---------------------------------------------------------------------------


def compare_non_lyrics(
    original: Id3Layout,
    new: Id3Layout,
    *,
    clear_fields: Iterable[str] = ID3_LYRICS_FRAMES,
) -> list[str]:
    """比较除歌词帧外的所有内容，返回差异描述（空列表表示完全一致）。

    这是「只动了歌词」的**可验证证明**，用于写入后校验。填充区允许变化
    （歌词变长必然需要调整填充），但版本、非歌词帧的原始字节、音频数据段与
    尾部标签必须完全一致。
    """
    problems: list[str] = []

    if (original.version, original.revision) != (new.version, new.revision):
        problems.append(
            f"ID3 主版本被改动: 2.{original.version}.{original.revision} "
            f"→ 2.{new.version}.{new.revision}"
        )
    if original.flags != new.flags:
        problems.append(f"ID3 标签级标志被改动: 0x{original.flags:02x} → 0x{new.flags:02x}")

    drop = {frame_id.upper() for frame_id in clear_fields}
    orig_frames = [f for f in original.frames if f.frame_id.upper() not in drop]
    new_frames = [f for f in new.frames if f.frame_id.upper() not in drop]

    if len(orig_frames) != len(new_frames):
        problems.append(f"非歌词帧数量变化: {len(orig_frames)} → {len(new_frames)}")
    else:
        for position, (orig_frame, new_frame) in enumerate(
            zip(orig_frames, new_frames, strict=True)
        ):
            if orig_frame.frame_id != new_frame.frame_id:
                problems.append(
                    f"第 {position} 个非歌词帧标识变化: "
                    f"{orig_frame.frame_id} → {new_frame.frame_id}"
                )
            elif orig_frame.raw != new_frame.raw:
                problems.append(
                    f"非歌词帧 {orig_frame.frame_id} 的原始字节被改动"
                    f"（{len(orig_frame.raw)} → {len(new_frame.raw)} 字节）"
                )

    if original.audio_sha256 != new.audio_sha256:
        problems.append("MPEG 音频数据哈希与原始不一致")
    if original.trailer != new.trailer:
        problems.append("尾部标签（ID3v1 / APEv2）被改动")

    return problems


# ---------------------------------------------------------------------------
# 封面（APIC）
# ---------------------------------------------------------------------------

#: 前面封面。与 FLAC 的 replace_type=3 语义对齐：只动同一类型的旧帧。
APIC_FRONT_COVER = 3


def parse_apic(body: bytes) -> tuple[str, int, str, bytes] | None:
    """解析 APIC 帧体 → (mime, picture_type, description, picture)。

    帧体：文本编码(1) + MIME(latin-1, NUL 结尾) + 图片类型(1)
          + 描述(按编码, NUL 结尾) + 图片数据。
    结构不完整返回 None（逐帧宽松：坏帧不该让整个标签不可读）。
    """
    if len(body) < 4:
        return None
    encoding = body[0]
    mime_end = body.find(b"\x00", 1)
    if mime_end < 0:
        return None
    mime = body[1:mime_end].decode("latin-1", "replace")
    if mime_end + 1 >= len(body):
        return None
    picture_type = body[mime_end + 1]
    description, position = _decode_terminated(body, mime_end + 2, encoding)
    return mime, picture_type, description, body[position:]


def _build_apic_frame(
    picture: bytes,
    *,
    mime: str,
    picture_type: int = APIC_FRONT_COVER,
    version: int,
) -> Id3Frame:
    """构造一个 APIC 帧。

    文本编码固定 latin-1、描述置空：MIME 本身就是 ASCII，空描述无损，
    这是各写入器的通行做法，互操作性最好。
    """
    mime_bytes = mime.encode("latin-1")
    body = (
        bytes((_ENC_LATIN1,))
        + mime_bytes
        + b"\x00"
        + bytes((picture_type,))
        + b"\x00"
        + picture
    )
    return Id3Frame("APIC", _build_frame("APIC", body, version))


def id3_rewrite_cover(
    source: Path,
    target: Path,
    *,
    picture: bytes | None,
    mime: str = "image/jpeg",
    picture_type: int = APIC_FRONT_COVER,
) -> None:
    """替换或移除 MP3 封面（APIC 帧），其余部分逐字节保留。

    与 FLAC 的封面语义一致：只替换同一 picture_type 的旧帧，其它类型
    （图标、艺人照等）原样保留；`picture=None` 表示移除该类型。
    保持原有 ID3v2 主版本；没有 ID3v2 时新建 v2.4（与标签写入一致）。
    target 必须与 source 在同一文件系统上，以便之后原子替换。
    """
    try:
        layout = id3_parse_file(source)
    except Id3FormatError as exc:
        if "没有 ID3v2 标签" not in str(exc):
            raise
        layout = _layout_without_tag(source)

    try:
        mime.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(f"MIME 必须是 latin-1 可编码: {mime!r}") from exc

    kept: list[bytes] = []
    for frame in layout.frames:
        if frame.frame_id == "APIC":
            parsed = parse_apic(frame.body)
            if parsed is not None and parsed[1] == picture_type:
                continue  # 同类型旧封面被替换/移除
        kept.append(frame.raw)
    if picture is not None:
        kept.append(
            _build_apic_frame(
                picture, mime=mime, picture_type=picture_type, version=layout.version
            ).raw
        )

    frames_bytes = b"".join(kept)
    original_body_size = layout.tag_size - 10 if layout.tag_size else 0
    if original_body_size and len(frames_bytes) <= original_body_size:
        padding = original_body_size - len(frames_bytes)
    else:
        padding = NEW_TAG_PADDING

    body = frames_bytes + b"\x00" * padding
    header = (
        ID3_MAGIC
        + bytes((layout.version, layout.revision, layout.flags))
        + _to_synchsafe(len(body))
    )

    audio_length = layout.audio_end - layout.audio_offset
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(header)
        dst.write(body)
        src.seek(layout.audio_offset)
        remaining = audio_length
        while remaining > 0:
            chunk = src.read(min(_COPY_CHUNK, remaining))
            if not chunk:
                raise Id3FormatError("复制音频数据时源文件提前结束")
            dst.write(chunk)
            remaining -= len(chunk)
        dst.write(layout.trailer)
        dst.flush()
        os.fsync(dst.fileno())
