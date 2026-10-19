"""FLAC 元数据的字节级受限写入。

**为什么不用 mutagen 的 FLAC.save()**：

曲库的文件里存在**重复的 Vorbis comment 键**（实际样例中 `ARTISTS`、
`ALBUMARTISTS`、`MUSICBRAINZ_ARTISTID` 各出现两次）以及多值键。
mutagen 的 `VComment` 按键归组，无法保证跨键的**原始交错顺序**不变；
直接 `save()` 后无法证明「只动了歌词」。

因此本模块直接操作 FLAC 的块结构：

    "fLaC" + [块头(1B 最后标志+类型) + 3B 长度 + 载荷] ... + 音频帧

- 除 VORBIS_COMMENT 外的所有块**逐字节保留**（含顺序、PICTURE、SEEKTABLE、
  CUESHEET、APPLICATION、未知块、STREAMINFO）
- VORBIS_COMMENT 内部：**逐条**保留原有注释项的原始字节与顺序（含重复键），
  只增删歌词项
- 音频帧区域**逐字节保留**

这样「音频与全部非歌词元数据不变」是可以**被验证**的，而不是被声称的。
"""

from __future__ import annotations

import hashlib
import shutil
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FLAC_MAGIC",
    "BlockType",
    "FlacLayout",
    "VorbisComments",
    "flac_audio_hash",
    "flac_parse_file",
    "flac_rewrite_with_lyrics",
    "has_id3_prefix",
    "lyrics_field_key",
]

FLAC_MAGIC = b"fLaC"
_COPY_CHUNK = 1 << 20


class BlockType:
    STREAMINFO = 0
    PADDING = 1
    APPLICATION = 2
    SEEKTABLE = 3
    VORBIS_COMMENT = 4
    CUESHEET = 5
    PICTURE = 6


class FlacFormatError(Exception):
    """结构不符合 FLAC 规范，或含本实现不支持的结构。"""


@dataclass(frozen=True, slots=True)
class VorbisComments:
    """Vorbis comment 块的结构化视图。

    `entries` 保留**原始顺序与重复项**，每项是 (字段名, 完整原始行字节)。
    重建时只增删歌词项，其余按原字节照抄。
    """

    vendor: bytes
    entries: tuple[tuple[bytes, bytes], ...]
    """(大写字段名, `KEY=value` 的原始字节)"""

    def get(self, key: str) -> list[bytes]:
        upper = key.upper().encode("ascii")
        return [value for name, raw in self.entries if name == upper and (value := _entry_value(raw)) is not None]

    def all_tags(self) -> dict[str, list[str]]:
        """全部字段（含重复项与多值），键为大写，顺序按原文件。

        与 `formats._MATCH_TAG_KEYS` 的区别：那个只返回**匹配所需**的字段，
        这里返回全部——标签编辑器必须能看到并修改每一个字段，
        否则用户会遇到「界面上没有这个标签，所以我改不了」。
        """
        out: dict[str, list[str]] = {}
        for name, raw in self.entries:
            value = _entry_value(raw)
            if value is None:
                continue
            out.setdefault(name.decode("ascii", "replace"), []).append(
                value.decode("utf-8", "replace")
            )
        return out

    def has(self, key: str) -> bool:
        upper = key.upper().encode("ascii")
        return any(name == upper for name, _ in self.entries)

    def without(self, keys: Iterable[str]) -> VorbisComments:
        drop = {k.upper().encode("ascii") for k in keys}
        return VorbisComments(
            vendor=self.vendor,
            entries=tuple((n, raw) for n, raw in self.entries if n not in drop),
        )

    def with_tags(
        self,
        tags: Mapping[str, Sequence[str]] | None = None,
        *,
        remove: Iterable[str] = (),
    ) -> VorbisComments:
        """按需替换 / 新增 / 删除**任意**字段。

        未涉及的字段**按原字节、原顺序保留**——这是这套写入器的根本承诺，
        改标签不能顺带重排或改写别的字段。

        替换时保留该字段原本出现的位置（第一处）；新增的追加到末尾。
        为什么在意顺序：曲库是用 Picard 整理的，同一专辑内字段顺序一致；
        把改过的字段挪到末尾会让那一个文件看起来"跟别人不一样"。

        `tags` 里同一字段给多个值就是多值（如 `ARTISTS` 有两个艺人）。
        """
        wanted: dict[bytes, list[str]] = {}
        for key, values in (tags or {}).items():
            wanted[key.strip().upper().encode("ascii")] = [str(v) for v in values]
        drop = {k.strip().upper().encode("ascii") for k in remove}
        emitted: set[bytes] = set()
        out: list[tuple[bytes, bytes]] = []

        for name, raw in self.entries:
            if name in drop:
                continue
            if name in wanted:
                if name not in emitted:
                    emitted.add(name)
                    for value in wanted[name]:
                        out.append((name, name + b"=" + value.encode("utf-8")))
                continue  # 同字段的后续重复项由写入的值取代
            out.append((name, raw))

        for name, values in wanted.items():
            if name in emitted:
                continue
            for value in values:
                out.append((name, name + b"=" + value.encode("utf-8")))
        return VorbisComments(vendor=self.vendor, entries=tuple(out))

    def with_entry(self, key: str, value: str) -> VorbisComments:
        upper = key.upper().encode("ascii")
        raw = upper + b"=" + value.encode("utf-8")
        return VorbisComments(vendor=self.vendor, entries=(*self.entries, (upper, raw)))

    def to_bytes(self) -> bytes:
        out = bytearray()
        out += struct.pack("<I", len(self.vendor))
        out += self.vendor
        out += struct.pack("<I", len(self.entries))
        for _name, raw in self.entries:
            out += struct.pack("<I", len(raw))
            out += raw
        return bytes(out)

    @classmethod
    def from_bytes(cls, payload: bytes) -> VorbisComments:
        view = memoryview(payload)
        offset = 0

        def read_u32() -> int:
            nonlocal offset
            if offset + 4 > len(payload):
                raise FlacFormatError("Vorbis comment 块截断（读取长度字段）")
            value = struct.unpack_from("<I", view, offset)[0]
            offset += 4
            return value

        vendor_len = read_u32()
        if offset + vendor_len > len(payload):
            raise FlacFormatError("Vorbis comment 块截断（vendor 字符串）")
        vendor = bytes(view[offset : offset + vendor_len])
        offset += vendor_len

        count = read_u32()
        # 防御性上限：避免损坏文件里的天文数字导致长时间循环
        if count > 1_000_000:
            raise FlacFormatError(f"Vorbis comment 项数异常: {count}")

        entries: list[tuple[bytes, bytes]] = []
        for _ in range(count):
            length = read_u32()
            if offset + length > len(payload):
                raise FlacFormatError("Vorbis comment 块截断（注释项）")
            raw = bytes(view[offset : offset + length])
            offset += length
            name = raw.split(b"=", 1)[0].upper() if b"=" in raw else raw.upper()
            entries.append((name, raw))
        return cls(vendor=vendor, entries=tuple(entries))


def _entry_value(raw: bytes) -> bytes | None:
    if b"=" not in raw:
        return None
    return raw.split(b"=", 1)[1]


@dataclass(slots=True)
class FlacLayout:
    """FLAC 文件的块布局。"""

    blocks: list[tuple[int, bytes]]
    audio_offset: int
    file_size: int

    def block(self, block_type: int) -> bytes | None:
        for btype, payload in self.blocks:
            if btype == block_type:
                return payload
        return None

    def comments(self) -> VorbisComments | None:
        payload = self.block(BlockType.VORBIS_COMMENT)
        return VorbisComments.from_bytes(payload) if payload is not None else None

    def audio_hash(self, path: Path) -> str:
        """压缩音频数据段的 SHA-256。

        有意**不**使用 STREAMINFO 里的 MD5：文件损坏时该值可能未被更新，
        因此必须对实际字节求哈希。
        """
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            fh.seek(self.audio_offset)
            while chunk := fh.read(_COPY_CHUNK):
                h.update(chunk)
        return h.hexdigest()


def has_id3_prefix(path: Path) -> bool:
    """FLAC 文件前置了 ID3 标签（非标准，一些老工具会写）。

    这是「依赖文件布局」的结构，本实现拒绝提交而不是猜着改。
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(3) == b"ID3"
    except OSError:
        return False


def flac_parse_file(path: Path) -> FlacLayout:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        return _parse(fh, size, str(path))


def flac_parse_bytes(data: bytes) -> FlacLayout:
    import io

    return _parse(io.BytesIO(data), len(data), "<bytes>")


def _parse(fh, size: int, label: str) -> FlacLayout:
    magic = fh.read(4)
    if magic != FLAC_MAGIC:
        raise FlacFormatError(f"{label}: 不是 FLAC（magic={magic!r}）")

    blocks: list[tuple[int, bytes]] = []
    offset = 4
    while True:
        header = fh.read(4)
        if len(header) != 4:
            raise FlacFormatError(f"{label}: 元数据块头截断")
        offset += 4
        is_last = bool(header[0] & 0x80)
        block_type = header[0] & 0x7F
        length = int.from_bytes(header[1:4], "big")
        if block_type == 127:
            # 127 是「无效」块类型，规范禁止
            raise FlacFormatError(f"{label}: 非法元数据块类型 127")
        payload = fh.read(length)
        if len(payload) != length:
            raise FlacFormatError(f"{label}: 元数据块载荷截断")
        offset += length
        if block_type == BlockType.STREAMINFO and len(payload) != 34:
            raise FlacFormatError(f"{label}: STREAMINFO 长度异常 {len(payload)}")
        blocks.append((block_type, payload))
        if is_last:
            break

    if not any(btype == BlockType.STREAMINFO for btype, _ in blocks):
        raise FlacFormatError(f"{label}: 缺少 STREAMINFO 块")

    audio_offset = offset
    if audio_offset >= size:
        raise FlacFormatError(f"{label}: 没有音频帧数据")
    return FlacLayout(blocks=blocks, audio_offset=audio_offset, file_size=size)


def flac_audio_hash(path: Path) -> str:
    return flac_parse_file(path).audio_hash(path)


def lyrics_field_key(name: str) -> str:
    from .formats import VORBIS_LYRICS_FIELDS

    normalised = name.strip().upper()
    if normalised not in VORBIS_LYRICS_FIELDS:
        raise ValueError(
            f"{name!r} 不在歌词字段白名单内。"
            "禁止用子串匹配判断歌词字段（LYRICIST 是作词人，不是歌词）。"
        )
    return normalised


def flac_rewrite_with_lyrics(
    source: Path,
    target: Path,
    *,
    lyrics: str | None,
    lyrics_field: str = "LYRICS",
    clear_fields: Iterable[str] = ("LYRICS", "UNSYNCEDLYRICS"),
    keep_padding: bool = True,
) -> None:
    """把歌词写入 FLAC 的 Vorbis comment，其余部分逐字节保留。

    lyrics=None 表示**只清理**白名单内的歌词项，不写入新歌词。

    target 必须与 source 在**同一文件系统**上，以便之后原子替换。
    """
    field = lyrics_field_key(lyrics_field)
    layout = flac_parse_file(source)
    comments = layout.comments()
    if comments is None:
        # 没有 VORBIS_COMMENT 块时，插入一个（放在 PADDING 之前以贴近惯例）
        comments = VorbisComments(vendor=b"lyra", entries=())

    updated = comments.without(clear_fields)
    if lyrics is not None:
        updated = updated.with_entry(field, lyrics)

    new_blocks = _blocks_with_comments(layout, updated, keep_padding=keep_padding)
    _write_flac(source, target, new_blocks, layout.audio_offset)


def _blocks_with_comments(
    layout: FlacLayout, comments: VorbisComments, *, keep_padding: bool
) -> list[tuple[int, bytes]]:
    """用新的 VORBIS_COMMENT 替换旧块，**其余块（含 PICTURE）原字节保留**。"""
    payload = comments.to_bytes()
    new_blocks: list[tuple[int, bytes]] = []
    placed = False
    for btype, block_payload in layout.blocks:
        if btype == BlockType.VORBIS_COMMENT:
            new_blocks.append((BlockType.VORBIS_COMMENT, payload))
            placed = True
        else:
            new_blocks.append((btype, block_payload))
    if not placed:
        # 插到 STREAMINFO 之后，保持「STREAMINFO 必须是第一块」的惯例
        new_blocks.insert(1, (BlockType.VORBIS_COMMENT, payload))
    if not keep_padding:
        new_blocks = [(t, b) for t, b in new_blocks if t != BlockType.PADDING]
    return new_blocks


def flac_rewrite_tags(
    source: Path,
    target: Path,
    *,
    tags: Mapping[str, Sequence[str]] | None = None,
    remove: Iterable[str] = (),
    keep_padding: bool = True,
) -> None:
    """改写 FLAC 的**任意** Vorbis comment 字段，音频数据逐字节保留。

    `tags` 中的字段被整体替换（不是追加），`remove` 中的被删除，
    其余字段按原字节、原顺序保留。封面（PICTURE 块）不受影响。
    """
    layout = flac_parse_file(source)
    comments = layout.comments() or VorbisComments(vendor=b"lyra", entries=())
    updated = comments.with_tags(tags, remove=remove)
    new_blocks = _blocks_with_comments(layout, updated, keep_padding=keep_padding)
    _write_flac(source, target, new_blocks, layout.audio_offset)


def _write_flac(
    source: Path,
    target: Path,
    blocks: list[tuple[int, bytes]],
    audio_offset: int,
) -> None:
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(FLAC_MAGIC)
        for index, (btype, payload) in enumerate(blocks):
            is_last = index == len(blocks) - 1
            header = bytes([(0x80 if is_last else 0x00) | (btype & 0x7F)])
            dst.write(header)
            dst.write(len(payload).to_bytes(3, "big"))
            dst.write(payload)
        src.seek(audio_offset)
        shutil.copyfileobj(src, dst, _COPY_CHUNK)
        dst.flush()
        import os

        os.fsync(dst.fileno())


def picture_type_of(payload: bytes) -> int | None:
    """从 PICTURE 负载里读出图片类型编号（前 4 字节，大端）。"""
    if len(payload) < 4:
        return None
    return struct.unpack_from(">I", payload, 0)[0]


def flac_rewrite_cover(
    source: Path,
    target: Path,
    *,
    picture: bytes | None,
    replace_type: int = 3,
    keep_padding: bool = True,
) -> None:
    """替换 / 移除 FLAC 的封面。

    `picture` 为已构造好的 PICTURE 负载（见 `cover.build_picture_block`）。
    - 传入图片：删除**同类型**的旧封面（默认 3 = 正面封面），追加新的；
      其它类型的图片（封底、艺人照等）原字节保留。
    - 传 None：删除该类型的全部封面。

    **音频数据与 VORBIS_COMMENT 逐字节保留**——改封面不该顺带动标签。

    为什么只替换同类型而不是清空所有图片：曲库里不少专辑同时有
    cover (1)~(4).jpg 这类多图，用户的意图通常是换正面封面，
    而不是把封底和艺人照一起删掉。
    """
    layout = flac_parse_file(source)

    kept: list[tuple[int, bytes]] = []
    for btype, payload in layout.blocks:
        if btype != BlockType.PICTURE:
            kept.append((btype, payload))
            continue
        if picture_type_of(payload) == replace_type:
            continue  # 旧封面丢弃
        kept.append((btype, payload))

    if picture is not None:
        # 插到 VORBIS_COMMENT 之后（惯例位置），没有就插到 STREAMINFO 之后
        insert_at = 1
        for index, (btype, _payload) in enumerate(kept):
            if btype == BlockType.VORBIS_COMMENT:
                insert_at = index + 1
                break
        kept.insert(insert_at, (BlockType.PICTURE, picture))

    if not keep_padding:
        kept = [(t, b) for t, b in kept if t != BlockType.PADDING]

    _write_flac(source, target, kept, layout.audio_offset)


def compare_non_lyrics(
    original: FlacLayout,
    new: FlacLayout,
    *,
    clear_fields: Iterable[str],
) -> list[str]:
    """比较除歌词外的所有内容，返回差异描述（空列表表示完全一致）。

    这是「只动了歌词」的**可验证证明**，用于写入后校验。
    """
    problems: list[str] = []

    # 1) 块序列（类型 + 载荷）必须一致，VORBIS_COMMENT 除外
    orig_seq = [(t, p) for t, p in original.blocks if t != BlockType.VORBIS_COMMENT]
    new_seq = [(t, p) for t, p in new.blocks if t != BlockType.VORBIS_COMMENT]
    if len(orig_seq) != len(new_seq):
        problems.append(
            f"元数据块数量变化: {len(orig_seq)} → {len(new_seq)}"
        )
    else:
        for idx, ((ot, op), (nt, np)) in enumerate(zip(orig_seq, new_seq, strict=True)):
            if ot != nt:
                problems.append(f"第 {idx} 个块类型变化: {ot} → {nt}")
            elif op != np:
                problems.append(f"块类型 {ot} 的载荷被改动（{len(op)} → {len(np)} 字节）")

    # 2) VORBIS_COMMENT 内：除歌词项外，逐条必须完全一致（含顺序与重复键）
    oc = original.comments()
    nc = new.comments()
    if oc is None or nc is None:
        problems.append("Vorbis comment 块缺失")
        return problems

    if oc.vendor != nc.vendor:
        problems.append("vendor 字符串被改动")

    drop = {f.upper().encode("ascii") for f in clear_fields}
    o_entries = [(n, raw) for n, raw in oc.entries if n not in drop]
    n_entries = [(n, raw) for n, raw in nc.entries if n not in drop]
    if len(o_entries) != len(n_entries):
        problems.append(f"非歌词注释项数量变化: {len(o_entries)} → {len(n_entries)}")
    else:
        for idx, ((_on, oraw), (_nn, nraw)) in enumerate(zip(o_entries, n_entries, strict=True)):
            if oraw != nraw:
                problems.append(
                    f"第 {idx} 条非歌词注释被改动: {oraw[:60]!r} → {nraw[:60]!r}"
                )
    return problems
