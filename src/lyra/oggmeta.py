"""OGG Vorbis / OGG Opus 元数据的字节级受限写入。

**为什么不用 mutagen 的 `OggVorbis.save()` / `OggOpus.save()`**：

与 FLAC 相同的理由——曲库里存在**重复的 Vorbis comment 键**，而 mutagen 的
`VCommentDict` 按键归组，无法保证跨键的**原始交错顺序**不变；直接 `save()`
之后无法证明「只动了歌词」。mutagen 还会整体重新分页，把「音频页面逐字节不变」
这一可验证性质丢掉。

本模块的约定与 `flacmeta.py` 对齐：

1. **逐条保留注释项的原始字节与顺序（含重复键）**，只增删调用方指定的字段。
   注释块的二进制格式（小端 vendor/条目长度表）与 FLAC 的 VORBIS_COMMENT 完全
   相同，因此这里**直接复用** `flacmeta.VorbisComments` 的编解码，避免两份实现
   在「重复键处理」上产生分歧。歌词与通用标签共用 `ogg_rewrite_tags`。
2. **音频包逐字节不变**：除承载 comment header 的包以外，所有包的内容一字不改。
3. **未参与重排的页面按文件原始字节复制**，不经由本模块的渲染器，避免
   「渲染器有细微 bug 就损坏用户文件」的风险。

## 本模块必须自己实现的部分：OGG 页面与 CRC

- 页面结构见 RFC 3533：`OggS` + 版本 + header type + granule + serial + sequence
  + CRC + 段表 + 段数据。
- **CRC 是这里最容易写错的地方**：多项式 `0x04c11db7`、初值 0、**不反射**、
  不做最终异或，计算时把 CRC 字段本身置零。多算一位或少算一位都会让播放器
  直接拒绝解码。
- 正确性不靠自证：见 `tests/test_oggmeta.py` 中的
  `test_page_render_roundtrip_matches_real_files`，它对真实 ffmpeg 产物逐页
  比对「按解析结果重新渲染」与「文件里的原始字节」，两者必须完全相等。

## 分页区域

实际 ffmpeg 产物的分页是：

    page#0  BOS      identification header（Vorbis 的 \\x01vorbis / Opus 的 OpusHead）
    page#1           comment header **与 setup header 同页**（Vorbis）
    page#2+          音频页

也就是说 comment header **并不独占一页**。因此本模块不「替换一个包」，而是找出
comment 包所在的**页面区域**，把该区域内的所有包重新分页。区域会做闭包扩展，
直到没有包跨越区域边界为止。若闭包结果会包含音频包，则**明确拒绝**：重新分页
音频需要知道每个包的 granule，而 OGG 只在页级记录 granule，无法无损推断。

## 已知取舍：解析时会占用约一个文件大小的内存

`ogg_parse_file` 会把整份文件读进内存并保留每一页的段数据。这是因为：
逐页重排与「未参与重排的页面按原始字节复制」都要求随时能拿到原页字节。

对本项目的规模（单曲普遍 20~60 MB，且 要求串行处理、限制在途
副本数量）是可以接受的。若将来要处理超大盘，可改为「页面只记偏移、按需 seek
读取」，但那样 `compare_non_lyrics` 就不能再是纯内存比较，需要一并调整。

包本身**不在内存中保留内容**（只记 span），否则会再多吃一份文件大小的内存。
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import flacmeta
from .formats import VORBIS_LYRICS_FIELDS

__all__ = [
    "MAX_PAGE_SEGMENTS",
    "OggFormatError",
    "OggLayout",
    "OggPacket",
    "OggPage",
    "compare_non_lyrics",
    "ogg_audio_hash",
    "ogg_crc32",
    "ogg_parse_bytes",
    "ogg_parse_file",
    "ogg_rewrite_tags",
    "ogg_rewrite_with_lyrics",
]

OGG_CAPTURE = b"OggS"
_CODEC_VORBIS = "vorbis"
_CODEC_OPUS = "opus"
_VORBIS_ID_SIG = b"\x01vorbis"
_VORBIS_COMMENT_SIG = b"\x03vorbis"
_OPUS_HEAD_SIG = b"OpusHead"
_OPUS_TAGS_SIG = b"OpusTags"

#: 单页段表最多 255 项（段表长度是 1 字节）
MAX_PAGE_SEGMENTS = 255
#: 单个 lacing 值的最大值
_MAX_LACING = 255
#: 一页段数据上限 = 255 段 × 255 字节
MAX_PAGE_DATA = MAX_PAGE_SEGMENTS * _MAX_LACING

_COPY_CHUNK = 1 << 20

# Vorbis comment 头部签名长度与结尾 framing bit 长度
_VORBIS_COMMENT_PREFIX = len(_VORBIS_COMMENT_SIG)
_OPUS_TAGS_PREFIX = len(_OPUS_TAGS_SIG)
_VORBIS_FRAMING_BYTES = 1


class OggFormatError(Exception):
    """结构不符合 OGG 规范，或含本实现不支持的结构。"""


# ---------------------------------------------------------------------------
# CRC-32（OGG 专用：多项式 0x04c11db7，初值 0，不反射，无最终异或）
# ---------------------------------------------------------------------------


def _build_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        remainder = index << 24
        for _ in range(8):
            if remainder & 0x80000000:
                remainder = ((remainder << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                remainder = (remainder << 1) & 0xFFFFFFFF
        table.append(remainder)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def ogg_crc32(data: bytes) -> int:
    """OGG 页面 CRC-32。

    注意这不是 zlib.crc32：OGG 用的是 **反射前**（MSB-first）的 CRC-32，
    多项式 0x04c11db7，初值 0，且不做最终异或。用 zlib 需要先做 bit-swap，
    mutagen 内部正是那么绕的；这里直接按规范实现，避免依赖位序转换。
    """
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


# ---------------------------------------------------------------------------
# 页面与包
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OggPage:
    """一个 OGG 页面。`data` 只含段数据，不含 26 字节页头与段表。"""

    index: int
    offset: int
    """页面在文件中的起始偏移。"""
    header_type: int
    granule: int
    serial: int
    sequence: int
    crc: int
    lacing: tuple[int, ...]
    data: bytes

    @property
    def total_size(self) -> int:
        return 27 + len(self.lacing) + len(self.data)

    @property
    def continued(self) -> bool:
        """首页是否承接上一页未结束的包。"""
        return bool(self.header_type & 0x01)

    @property
    def first(self) -> bool:
        """是否逻辑流的第一个页面（BOS）。"""
        return bool(self.header_type & 0x02)

    @property
    def last(self) -> bool:
        """是否逻辑流的最后一个页面（EOS）。"""
        return bool(self.header_type & 0x04)

    def render(self, *, sequence: int | None = None) -> bytes:
        """按字段重新渲染页面字节并重算 CRC。

        `test_page_render_roundtrip_matches_real_files` 断言它与真实文件里的
        原始字节完全一致——这是 CRC 实现正确性的直接证据。
        """
        return _render_page(
            header_type=self.header_type,
            granule=self.granule,
            serial=self.serial,
            sequence=self.sequence if sequence is None else sequence,
            segments=self.lacing,
            data=self.data,
        )


@dataclass(frozen=True, slots=True)
class OggPacket:
    """一个逻辑包。

    `spans` 是 `(页面索引, 段数据内偏移, 长度)` 的序列；包可以跨页，所以内容
    在文件里并不是连续的。这里**不保留包内容**，音频包动辄几十万个，全留下会
    白白占用一个文件大小的内存。
    """

    index: int
    spans: tuple[tuple[int, int, int], ...]
    length: int
    start_page: int
    end_page: int


def _render_page(
    *,
    header_type: int,
    granule: int,
    serial: int,
    sequence: int,
    segments: Iterable[int],
    data: bytes,
) -> bytes:
    segments = tuple(segments)
    if len(segments) > MAX_PAGE_SEGMENTS:
        raise OggFormatError(f"段表项数超限: {len(segments)}")
    if sum(segments) != len(data):
        raise OggFormatError(
            f"段表与段数据不一致: lacing 合计 {sum(segments)} != 数据 {len(data)}"
        )

    head = bytearray()
    head += OGG_CAPTURE
    head += bytes((0, header_type & 0xFF))
    head += struct.pack("<q", granule)
    head += struct.pack("<I", serial)
    head += struct.pack("<I", sequence)
    head += b"\x00\x00\x00\x00"  # CRC 占位，算完再填
    head += bytes((len(segments),))
    head += bytes(segments)

    page = bytes(head) + data
    crc = ogg_crc32(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def _build_packets(pages: list[OggPage], label: str) -> list[OggPacket]:
    """按段表的 lacing 规则把页面还原成包序列。"""
    packets: list[OggPacket] = []
    spans: list[tuple[int, int, int]] = []
    length = 0
    start_page: int | None = None

    for page in pages:
        offset = 0
        for lacing in page.lacing:
            if start_page is None:
                start_page = page.index
            if lacing > 0:
                spans.append((page.index, offset, lacing))
                length += lacing
                offset += lacing
            if lacing < _MAX_LACING:
                # 一个 <255 的 lacing 值结束当前包（包括长度为 0 的空包）
                packets.append(
                    OggPacket(
                        index=len(packets),
                        spans=tuple(spans),
                        length=length,
                        start_page=start_page,
                        end_page=page.index,
                    )
                )
                spans = []
                length = 0
                start_page = None

    if spans:
        raise OggFormatError(f"{label}: 最后一个包不完整（文件被截断）")
    if not packets:
        raise OggFormatError(f"{label}: 没有任何逻辑包")
    return packets


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OggLayout:
    """OGG 文件的页面、包与 comment header 视图。"""

    codec: str
    pages: list[OggPage]
    packets: list[OggPacket]
    comment_index: int
    header_packet_count: int
    audio_sha256: str
    serial: int
    file_size: int
    comments: flacmeta.VorbisComments
    comment_padding: bytes
    """注释块之后的尾部字节，原样保留。

    Vorbis：framing bit 之后的部分；Opus：注释块之后的部分。
    保留它是为了「尽量不动其它东西」——不少工具（如 mutagen）会有意在
    comment packet 里留填充，以便日后原地更新标签。丢掉填充会让文件尺寸
    发生无谓变化。
    """

    # -- 包内容 --

    def packet_bytes(self, packet: OggPacket) -> bytes:
        out = bytearray()
        for page_index, offset, length in packet.spans:
            out += self.pages[page_index].data[offset : offset + length]
        return bytes(out)

    def comment_packet_bytes(self) -> bytes:
        return self.packet_bytes(self.packets[self.comment_index])

    def non_comment_header_packets(self) -> list[tuple[int, bytes]]:
        """除 comment 之外的头部包（编解码器识别头、Vorbis setup 头等）。"""
        out: list[tuple[int, bytes]] = []
        for packet in self.packets[: self.header_packet_count]:
            if packet.index == self.comment_index:
                continue
            out.append((packet.index, self.packet_bytes(packet)))
        return out

    # -- 重写 --

    def render_comment_packet(self, comments: flacmeta.VorbisComments) -> bytes:
        """把注释块重新封装成 comment header 包。

        - Vorbis：`\\x03vorbis` + 注释块 + **framing bit（取值 1）**，之后原样接回
          原有的尾部填充。
        - Opus：`OpusTags` + 注释块 + 原有的尾部填充；**没有 framing bit**。

        写错 framing bit 会让解码器直接拒绝整个流。
        """
        block = comments.to_bytes()
        if self.codec == _CODEC_VORBIS:
            return (
                _VORBIS_COMMENT_SIG
                + block
                + b"\x01" * _VORBIS_FRAMING_BYTES
                + self.comment_padding
            )
        return _OPUS_TAGS_SIG + block + self.comment_padding

    def comment_region(self) -> tuple[int, int, list[OggPacket]]:
        """comment 包所在的页面区域，以及需要一起重新分页的包。

        区域会闭包扩展：只要还有包跨越 `[start, end]` 边界，就把该包完全纳入，
        直到区域边界与包的边界对齐。这样区域内永远是「若干个完整的包」。
        """
        comment = self.packets[self.comment_index]
        start, end = comment.start_page, comment.end_page
        while True:
            included = self._packets_between(start, end)
            new_start = min(p.start_page for p in included)
            new_end = max(p.end_page for p in included)
            if new_start == start and new_end == end:
                break
            start, end = new_start, new_end
        return start, end, self._packets_between(start, end)

    def _packets_between(self, start: int, end: int) -> list[OggPacket]:
        return [p for p in self.packets if p.start_page <= end and p.end_page >= start]

    def audio_hash(self) -> str:
        """除 comment 包外，所有包内容按顺序的 SHA-256。

        comment 变长会改变其后的页面偏移与页面序号，但**包内容**不变，
        因此这个哈希对「只改了歌词」是稳定的。
        """
        return self.audio_sha256


def _compute_audio_hash(
    pages: list[OggPage], packets: list[OggPacket], skip_index: int
) -> str:
    digest = hashlib.sha256()
    for packet in packets:
        if packet.index == skip_index:
            continue
        for page_index, offset, length in packet.spans:
            digest.update(pages[page_index].data[offset : offset + length])
    return digest.hexdigest()


def _detect_codec(packets: list[OggPacket], pages: list[OggPage], label: str) -> str:
    first = packets[0]
    head = bytearray()
    for page_index, offset, length in first.spans:
        head += pages[page_index].data[offset : offset + length]
    if bytes(head).startswith(_VORBIS_ID_SIG):
        return _CODEC_VORBIS
    if bytes(head).startswith(_OPUS_HEAD_SIG):
        return _CODEC_OPUS
    raise OggFormatError(
        f"{label}: 不是 OGG Vorbis 也不是 OGG Opus（首包={bytes(head[:16])!r}）"
    )


def _comment_block_length(payload: bytes, label: str) -> int:
    """按 Vorbis comment 的结构算出注释块自身的长度。

    必须按结构算，不能假设「注释块一直延伸到包尾」：实际 mutagen 会在
    framing bit 之后补零填充（`packets[0] = vcomment_data + b"\\x00" * padding`），
    所以包尾既不是注释块末尾，也不一定是 framing bit。
    """
    offset = 0

    def read_u32(what: str) -> int:
        nonlocal offset
        if offset + 4 > len(payload):
            raise OggFormatError(f"{label}: 注释块截断（{what}）")
        value = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        return value

    vendor_length = read_u32("vendor 长度")
    if offset + vendor_length > len(payload):
        raise OggFormatError(f"{label}: 注释块截断（vendor 字符串）")
    offset += vendor_length

    count = read_u32("注释项数")
    if count > 1_000_000:
        raise OggFormatError(f"{label}: 注释项数异常 {count}")

    for index in range(count):
        length = read_u32(f"第 {index} 条长度")
        if offset + length > len(payload):
            raise OggFormatError(f"{label}: 注释块截断（第 {index} 条注释）")
        offset += length

    return offset


def _parse_comment(
    packet: bytes, codec: str, label: str
) -> tuple[flacmeta.VorbisComments, bytes]:
    """解析 comment header 包，返回 (注释块, 尾部填充字节)。"""
    if codec == _CODEC_VORBIS:
        if not packet.startswith(_VORBIS_COMMENT_SIG):
            raise OggFormatError(f"{label}: 第 2 个包不是 Vorbis comment header")
        body = packet[_VORBIS_COMMENT_PREFIX:]
        block_length = _comment_block_length(body, label)
        rest = body[block_length:]
        if not rest:
            raise OggFormatError(f"{label}: Vorbis comment header 缺少 framing bit")
        if not rest[0] & 0x01:
            raise OggFormatError(
                f"{label}: Vorbis comment header 的 framing bit 不为 1（{rest[0]:#04x}）"
            )
        padding = rest[_VORBIS_FRAMING_BYTES:]
    else:
        if not packet.startswith(_OPUS_TAGS_SIG):
            raise OggFormatError(f"{label}: 第 2 个包不是 OpusTags")
        body = packet[_OPUS_TAGS_PREFIX:]
        block_length = _comment_block_length(body, label)
        # OpusTags 没有 framing bit；其后的字节按约定是填充（首字节最低位为 1
        # 表示「存在填充」），这里不解释、只原样保留
        padding = body[block_length:]

    try:
        comments = flacmeta.VorbisComments.from_bytes(body[:block_length])
    except flacmeta.FlacFormatError as exc:
        # 复用 FLAC 的注释编解码器，但对外只暴露本模块的异常类型
        raise OggFormatError(f"{label}: comment header 解析失败: {exc}") from exc
    return comments, padding


def ogg_parse_file(path: Path) -> OggLayout:
    data = path.read_bytes()
    return _parse(data, str(path))


def ogg_parse_bytes(data: bytes, label: str = "<bytes>") -> OggLayout:
    return _parse(data, label)


def _parse(data: bytes, label: str) -> OggLayout:
    pages: list[OggPage] = []
    offset = 0
    total = len(data)

    while offset < total:
        if total - offset < 27:
            raise OggFormatError(f"{label}: 页头截断 @ {offset}")
        if data[offset : offset + 4] != OGG_CAPTURE:
            raise OggFormatError(
                f"{label}: 未找到 OggS 捕获模式 @ {offset}（{data[offset:offset + 4]!r}）"
            )
        version = data[offset + 4]
        if version != 0:
            raise OggFormatError(f"{label}: 不支持的 OGG 版本 {version}")
        header_type = data[offset + 5]
        granule = struct.unpack_from("<q", data, offset + 6)[0]
        serial = struct.unpack_from("<I", data, offset + 14)[0]
        sequence = struct.unpack_from("<I", data, offset + 18)[0]
        crc = struct.unpack_from("<I", data, offset + 22)[0]
        segment_count = data[offset + 26]
        lacing = tuple(data[offset + 27 : offset + 27 + segment_count])
        if len(lacing) != segment_count:
            raise OggFormatError(f"{label}: 段表截断 @ {offset}")

        data_offset = offset + 27 + segment_count
        payload_size = sum(lacing)
        if data_offset + payload_size > total:
            raise OggFormatError(f"{label}: 页数据截断 @ {offset}")

        pages.append(
            OggPage(
                index=len(pages),
                offset=offset,
                header_type=header_type,
                granule=granule,
                serial=serial,
                sequence=sequence,
                crc=crc,
                lacing=lacing,
                data=data[data_offset : data_offset + payload_size],
            )
        )
        offset = data_offset + payload_size

    if not pages:
        raise OggFormatError(f"{label}: 没有任何 OGG 页面")

    serials = {page.serial for page in pages}
    if len(serials) != 1:
        # 复用（multiplexed）或链接（chained）流：comment header 归属不唯一，
        # 无法安全地认定「该改哪一个」，明确拒绝而不是猜。
        raise OggFormatError(
            f"{label}: 含 {len(serials)} 条逻辑流（复用/链接流），本实现不支持"
        )

    packets = _build_packets(pages, label)
    codec = _detect_codec(packets, pages, label)

    if len(packets) < 2:
        raise OggFormatError(f"{label}: 缺少 comment header 包")

    header_packet_count = 3 if codec == _CODEC_VORBIS else 2
    if len(packets) < header_packet_count:
        raise OggFormatError(f"{label}: 头部包不完整（{codec} 需要 {header_packet_count} 个）")

    comments, comment_padding = _parse_comment(
        _packet_bytes(pages, packets[1]), codec, label
    )

    return OggLayout(
        codec=codec,
        pages=pages,
        packets=packets,
        comment_index=1,
        header_packet_count=header_packet_count,
        audio_sha256=_compute_audio_hash(pages, packets, skip_index=1),
        serial=pages[0].serial,
        file_size=total,
        comments=comments,
        comment_padding=comment_padding,
    )


def _packet_bytes(pages: list[OggPage], packet: OggPacket) -> bytes:
    out = bytearray()
    for page_index, offset, length in packet.spans:
        out += pages[page_index].data[offset : offset + length]
    return bytes(out)


def ogg_audio_hash(path: Path) -> str:
    return ogg_parse_file(path).audio_hash()


# ---------------------------------------------------------------------------
# 重新分页
# ---------------------------------------------------------------------------


def _paginate(
    packets: list[bytes],
    *,
    serial: int,
    start_sequence: int,
    bos: bool,
    eos: bool,
    first_continued: bool,
) -> list[bytes]:
    """把若干包重新分页，并逐页重算 CRC。

    granule 的赋值依据：区域内只允许是头部包，头部页的 granule 按 RFC 3533
    取 0（本页有包结束）或 -1（本页没有包结束）。
    """
    if not packets:
        raise OggFormatError("重新分页时没有任何包")

    rendered: list[bytes] = []
    segments: list[int] = []
    buffer = bytearray()
    completed = False
    page_continued = first_continued
    page_is_first = True
    sequence = start_sequence

    def flush(*, last: bool, next_continued: bool) -> None:
        nonlocal segments, buffer, completed, page_continued, page_is_first, sequence
        header_type = 0
        if page_continued:
            header_type |= 0x01
        if page_is_first and bos:
            header_type |= 0x02
        if last and eos:
            header_type |= 0x04
        rendered.append(
            _render_page(
                header_type=header_type,
                granule=0 if completed else -1,
                serial=serial,
                sequence=sequence,
                segments=segments,
                data=bytes(buffer),
            )
        )
        sequence += 1
        segments = []
        buffer = bytearray()
        completed = False
        page_continued = next_continued
        page_is_first = False

    for packet in packets:
        position = 0
        while True:
            if len(segments) >= MAX_PAGE_SEGMENTS:
                # 段表满了，本包必须续到下一页
                flush(last=False, next_continued=True)
            take = min(_MAX_LACING, len(packet) - position)
            segments.append(take)
            if take:
                buffer += packet[position : position + take]
            position += take
            if take < _MAX_LACING:
                completed = True
                break

    flush(last=True, next_continued=False)
    return rendered


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _validated_clear_fields(clear_fields: Iterable[str]) -> frozenset[str]:
    """只允许清理白名单内的歌词字段。

    这道校验是有意的防御：若有人传入 `("LYRICIST",)`，作词人信息会被清空，
    属于不可恢复的用户数据损失。宁可让调用方报错，也不要静默删掉。
    """
    allowed = {field.upper() for field in VORBIS_LYRICS_FIELDS}
    requested = {field.upper() for field in clear_fields}
    unknown = requested - allowed
    if unknown:
        raise ValueError(
            f"{sorted(unknown)} 不在歌词字段白名单 {sorted(allowed)} 内。"
            "禁止用子串匹配判断歌词字段（LYRICIST 是作词人，不是歌词）。"
        )
    return frozenset(requested)


def ogg_rewrite_with_lyrics(
    source: Path,
    target: Path,
    *,
    lyrics: str | None,
    clear_fields: Iterable[str] = VORBIS_LYRICS_FIELDS,
    lyrics_field: str = "LYRICS",
) -> None:
    """把歌词写入 OGG 的 Vorbis comment，其余部分逐字节保留。

    `lyrics=None` 表示**只清理**白名单内的歌词项，不写入新歌词。

    target 必须与 source 在**同一文件系统**上，以便之后原子替换。
    """
    field = flacmeta.lyrics_field_key(lyrics_field)
    drop = _validated_clear_fields(clear_fields)
    tags = {field: [lyrics]} if lyrics is not None else None
    ogg_rewrite_tags(source, target, tags=tags, remove=drop)


def _validated_tag_names(names: Iterable[str]) -> frozenset[str]:
    """标签名必须是 ASCII，且不允许空名或含 ``=`` / NUL。

    与 FLAC 的 Vorbis comment 完全相同的约束：字段名写坏会让整块注释无法解析。
    这里不做「白名单」限制——标签编辑器允许修改任意字段；只挡住结构上非法的名字。
    """
    out: set[str] = set()
    for name in names:
        upper = str(name).strip().upper()
        if not upper or "=" in upper or "\x00" in upper:
            raise ValueError(f"非法标签名: {name!r}")
        try:
            upper.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(f"标签名必须是 ASCII: {name!r}") from exc
        out.add(upper)
    return frozenset(out)


def ogg_rewrite_tags(
    source: Path,
    target: Path,
    *,
    tags: Mapping[str, Sequence[str]] | None = None,
    remove: Iterable[str] = (),
) -> None:
    """改写 OGG（Vorbis / Opus）的任意 Vorbis comment 字段。

    与 `flac_rewrite_tags` 语义一致：`tags` 中的字段整体替换，`remove` 中的字段
    删除，其余字段按原始字节与顺序保留；音频包、编解码器头逐字节不变。

    实现复用 `ogg_rewrite_with_lyrics` 的页面区域重排逻辑，区别只在
    「用 `with_tags` 而不是只增删歌词项」。因此它同样继承那条硬约束：
    comment 包与音频包同页、且无法无损分离时**明确拒绝**。
    """
    _validated_tag_names(tags or {})
    drop = _validated_tag_names(remove)
    layout = ogg_parse_file(source)
    updated = layout.comments.with_tags(tags, remove=drop)
    _rewrite_comment_packet(layout, updated, source=source, target=target)


def _rewrite_comment_packet(
    layout: OggLayout,
    updated: flacmeta.VorbisComments,
    *,
    source: Path,
    target: Path,
) -> None:
    """把已改好的注释块重新分页并写回，音频包逐字节保留。"""
    new_comment = layout.render_comment_packet(updated)

    start, end, region_packets = layout.comment_region()

    # 区域内必须全是头部包。若闭包把音频包卷了进来，重新分页就需要知道每个音频包
    # 的 granule，而 OGG 只在页级记录 granule —— 无法无损推断，明确拒绝。
    audio_packets = [p.index for p in region_packets if p.index >= layout.header_packet_count]
    if audio_packets:
        raise OggFormatError(
            "comment header 与音频包同页且无法安全分离（涉及音频包 "
            f"{audio_packets[:5]}）；拒绝重新分页，避免破坏 granule 时间信息"
        )

    for page in layout.pages[start : end + 1]:
        if page.granule not in (0, -1):
            raise OggFormatError(
                f"comment 区域第 {page.index} 页的 granule={page.granule} 非头部页取值；"
                "拒绝重新分页"
            )

    region_blobs = [
        new_comment
        if packet.index == layout.comment_index
        else layout.packet_bytes(packet)
        for packet in region_packets
    ]

    first_page = layout.pages[start]
    last_page = layout.pages[end]
    new_pages = _paginate(
        region_blobs,
        serial=layout.serial,
        start_sequence=first_page.sequence,
        bos=first_page.first,
        eos=last_page.last,
        first_continued=first_page.continued,
    )

    delta = len(new_pages) - (end - start + 1)
    _write_ogg(source, target, layout, start, end, new_pages, delta)


def _copy_exact(src, dst, offset: int, length: int) -> None:
    src.seek(offset)
    remaining = length
    while remaining > 0:
        chunk = src.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            raise OggFormatError("复制时源文件提前结束")
        dst.write(chunk)
        remaining -= len(chunk)


def _write_ogg(
    source: Path,
    target: Path,
    layout: OggLayout,
    start: int,
    end: int,
    new_pages: list[bytes],
    delta: int,
) -> None:
    pages = layout.pages
    with open(source, "rb") as src, open(target, "wb") as dst:
        # 区域之前的页：直接用文件原始字节，不经渲染器
        if start:
            _copy_exact(src, dst, 0, pages[start].offset)

        for blob in new_pages:
            dst.write(blob)

        for index in range(end + 1, len(pages)):
            page = pages[index]
            if delta:
                # 页面序号变了，CRC 必须重算；内容取自解析结果
                dst.write(page.render(sequence=page.sequence + delta))
            else:
                _copy_exact(src, dst, page.offset, page.total_size)

        dst.flush()
        os.fsync(dst.fileno())


# ---------------------------------------------------------------------------
# 可验证证明
# ---------------------------------------------------------------------------


def compare_non_lyrics(
    original: OggLayout,
    new: OggLayout,
    *,
    clear_fields: Iterable[str] = VORBIS_LYRICS_FIELDS,
) -> list[str]:
    """比较除歌词外的所有内容，返回差异描述（空列表表示完全一致）。

    这是「只动了歌词」的**可验证证明**，用于写入后校验。页面分页允许变化
    （comment 变长必然导致重新分页），但**包内容**必须一致。
    """
    problems: list[str] = []

    if original.codec != new.codec:
        problems.append(f"编解码器变化: {original.codec} → {new.codec}")
    if original.serial != new.serial:
        problems.append("逻辑流序号(serial)被改动")
    if original.comment_index != new.comment_index:
        problems.append("comment header 的包序号发生变化")

    # 1) 除 comment 外的头部包逐字节一致
    orig_headers = original.non_comment_header_packets()
    new_headers = new.non_comment_header_packets()
    if len(orig_headers) != len(new_headers):
        problems.append(f"头部包数量变化: {len(orig_headers)} → {len(new_headers)}")
    else:
        for (orig_index, orig_bytes), (new_index, new_bytes) in zip(
            orig_headers, new_headers, strict=True
        ):
            if orig_bytes != new_bytes:
                problems.append(
                    f"头部包[{orig_index}→{new_index}] 被改动"
                    f"（{len(orig_bytes)} → {len(new_bytes)} 字节）"
                )

    # 2) 注释块：vendor 与除歌词外的每一条都必须完全一致（含顺序与重复键）
    if original.comments.vendor != new.comments.vendor:
        problems.append("vendor 字符串被改动")

    drop = {field.upper().encode("ascii") for field in clear_fields}
    orig_entries = [
        (name, raw) for name, raw in original.comments.entries if name not in drop
    ]
    new_entries = [
        (name, raw) for name, raw in new.comments.entries if name not in drop
    ]
    if len(orig_entries) != len(new_entries):
        problems.append(f"非歌词注释项数量变化: {len(orig_entries)} → {len(new_entries)}")
    else:
        for position, ((_, orig_raw), (_, new_raw)) in enumerate(
            zip(orig_entries, new_entries, strict=True)
        ):
            if orig_raw != new_raw:
                problems.append(
                    f"第 {position} 条非歌词注释被改动: "
                    f"{orig_raw[:60]!r} → {new_raw[:60]!r}"
                )

    # 3) 音频（及所有非 comment 包）内容一致
    if original.audio_sha256 != new.audio_sha256:
        problems.append("非 comment 包的数据哈希与原始不一致")

    return problems
