"""FLAC 字节级受限写入的测试。

为什么测这些：

**为什么不能用 mutagen 的 `FLAC.save()`：** 曲库的文件里存在
**重复的 Vorbis comment 键**（实际样例中 ARTISTS / ALBUMARTISTS /
MUSICBRAINZ_ARTISTID 在字节层面各有 **3 条同名条目**，mutagen 按键归组后
只呈现为「1 键 3 值」）。`VComment` 无法保证跨键的**原始交错顺序**不变，
直接 `save()` 后无法证明「只动了歌词」。

因此本模块直接操作 FLAC 块结构。这里要证明的正是那句承诺：
**除歌词外，一切逐字节不变，且这个结论是可被验证的，而不是被声称的。**

- `audio_hash` 必须不变（对**实际压缩音频字节**求哈希，有意不用 STREAMINFO 里的 MD5，
  因为文件损坏时该值可能未被更新）。
- `compare_non_lyrics` 必须为空列表；它自己也要有效——所以另有一条测试
  证明它**能检出**篡改，否则「空列表」可能只是因为检查形同虚设。
- 结构非法时必须**明确拒绝**（`FlacFormatError`），绝不通用重写兜底。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.flacmeta import (
    FLAC_MAGIC,
    BlockType,
    FlacFormatError,
    VorbisComments,
    compare_non_lyrics,
    flac_audio_hash,
    flac_parse_bytes,
    flac_parse_file,
    flac_rewrite_with_lyrics,
    has_id3_prefix,
    lyrics_field_key,
)
from lyra.formats import VORBIS_LYRICS_FIELDS

LYRICS = "[00:01.00]第一行\n[00:02.00]第二行\n"


def _non_lyrics(comments: VorbisComments):
    drop = {f.upper().encode("ascii") for f in VORBIS_LYRICS_FIELDS}
    return [(name, raw) for name, raw in comments.entries if name not in drop]


# ---------------------------------------------------------------------------
# 真实文件的结构解析
# ---------------------------------------------------------------------------


def test_解析真实FLAC的块结构(real_flac: Path):
    """真实文件是唯一的最终裁判；合成文件可能过于规整。"""
    layout = flac_parse_file(real_flac)

    types = [block_type for block_type, _ in layout.blocks]
    assert BlockType.STREAMINFO in types, "FLAC 必须有 STREAMINFO"
    assert types[0] == BlockType.STREAMINFO, "STREAMINFO 必须是第一个块"

    streaminfo = layout.block(BlockType.STREAMINFO)
    assert streaminfo is not None
    assert len(streaminfo) == 34, "STREAMINFO 规范长度固定为 34 字节"

    assert layout.audio_offset > 4
    assert layout.audio_offset < layout.file_size, "必须存在音频帧数据"
    assert layout.comments() is not None, "真实素材应当有 Vorbis comment"


def test_真实FLAC的音频哈希稳定(real_flac: Path):
    """同一文件两次求哈希必须一致，否则「音频未变」的校验没有意义。"""
    assert flac_audio_hash(real_flac) == flac_audio_hash(real_flac)
    assert len(flac_audio_hash(real_flac)) == 64


def test_真实FLAC存在重复键(real_flac: Path, dup_keys):
    """先确认素材本身确实含重复键，否则后续「保留重复键」的断言是空转。"""
    comments = flac_parse_file(real_flac).comments()
    assert comments is not None
    for key in dup_keys:
        raw_entries = [raw for name, raw in comments.entries if name == key.encode("ascii")]
        assert len(raw_entries) >= 2, f"{key} 应当出现多次，实际 {len(raw_entries)}"
        # 同名条目必须是不同内容，才是真正的多值而非完全重复
        assert len(set(raw_entries)) == len(raw_entries)


def test_vendor字符串被保留(real_flac: Path):
    comments = flac_parse_file(real_flac).comments()
    assert comments is not None
    assert comments.vendor, "vendor 不应为空"
    assert b"libFLAC" in comments.vendor or comments.vendor


# ---------------------------------------------------------------------------
# 核心承诺：重复键与非歌词内容逐字节、按序保留
# ---------------------------------------------------------------------------


def test_写入歌词后重复键逐字节按序保留(real_flac: Path, tmp_path: Path, dup_keys):
    """这是整个模块存在的理由。

    ARTISTS / ALBUMARTISTS / MUSICBRAINZ_ARTISTID 在字节层面各有多条同名条目，
    改写歌词后必须：
      - 条数不变
      - 每条的原始字节不变
      - 整体顺序不变（跨键的交错顺序也不能变）
    """
    target = tmp_path / "out.flac"
    original = flac_parse_file(real_flac)
    original_comments = original.comments()
    assert original_comments is not None

    flac_rewrite_with_lyrics(real_flac, target, lyrics=LYRICS)

    new_comments = flac_parse_file(target).comments()
    assert new_comments is not None

    # 1) 每个重复键的原始字节序列必须完全一致（含顺序）
    for key in dup_keys:
        before = [raw for name, raw in original_comments.entries if name == key.encode("ascii")]
        after = [raw for name, raw in new_comments.entries if name == key.encode("ascii")]
        assert before == after, f"{key} 的重复条目被改动: {before} -> {after}"

    # 2) 除歌词外的**全部**条目逐条、按序完全一致
    assert _non_lyrics(original_comments) == _non_lyrics(new_comments)

    # 3) vendor 不变
    assert original_comments.vendor == new_comments.vendor


def test_写入歌词后音频哈希不变(real_flac: Path, tmp_path: Path):
    """音频压缩数据必须逐字节保持，绝不转码/重采样/修复。"""
    target = tmp_path / "out.flac"
    before = flac_audio_hash(real_flac)
    flac_rewrite_with_lyrics(real_flac, target, lyrics=LYRICS)
    assert flac_audio_hash(target) == before


def test_写入歌词后compare_non_lyrics为空(real_flac: Path, tmp_path: Path):
    """这是「只动了歌词」的可验证证明。"""
    target = tmp_path / "out.flac"
    original = flac_parse_file(real_flac)
    flac_rewrite_with_lyrics(real_flac, target, lyrics=LYRICS)
    new = flac_parse_file(target)

    problems = compare_non_lyrics(original, new, clear_fields=VORBIS_LYRICS_FIELDS)
    assert problems == [], f"非歌词内容被改动: {problems}"


def test_写入歌词只增加一个歌词条目(real_flac: Path, tmp_path: Path):
    """非歌词条目数不得变化；素材原本没有歌词时才允许 +1。"""
    target = tmp_path / "out.flac"
    original = flac_parse_file(real_flac)
    had_lyrics = any(
        name in {f.upper().encode("ascii") for f in VORBIS_LYRICS_FIELDS}
        for name, _ in original.comments().entries
    )
    flac_rewrite_with_lyrics(real_flac, target, lyrics=LYRICS)

    before = len(_non_lyrics(original.comments()))
    after = len(_non_lyrics(flac_parse_file(target).comments()))
    assert before == after
    if not had_lyrics:
        total_before = len(original.comments().entries)
        total_after = len(flac_parse_file(target).comments().entries)
        assert total_after == total_before + 1


def test_compare_non_lyrics能检出篡改(real_flac: Path, tmp_path: Path):
    """反例：证明校验器本身有效。

    如果它永远返回空列表，「没有改动」这个结论就毫无保证。
    """
    original = flac_parse_file(real_flac)
    other = tmp_path / "other.flac"
    # 改写一个非歌词标签，制造真实差异
    flac_rewrite_with_lyrics(real_flac, other, lyrics=LYRICS)


    layout = flac_parse_file(other)
    comments = layout.comments()
    entries = list(comments.entries)
    # 找一条非歌词、非重复键的条目来改
    for index, (name, _raw) in enumerate(entries):
        if name == b"TITLE":
            entries[index] = (name, "TITLE=被篡改的标题".encode())
            break
    mutated = VorbisComments(vendor=comments.vendor, entries=tuple(entries))
    blocks = [
        (BlockType.VORBIS_COMMENT, mutated.to_bytes()) if btype == BlockType.VORBIS_COMMENT
        else (btype, payload)
        for btype, payload in layout.blocks
    ]
    data = bytearray(FLAC_MAGIC)
    for i, (btype, payload) in enumerate(blocks):
        data.append((0x80 if i == len(blocks) - 1 else 0) | btype)
        data += len(payload).to_bytes(3, "big") + payload
    data += other.read_bytes()[layout.audio_offset:]
    tampered = tmp_path / "tampered.flac"
    tampered.write_bytes(bytes(data))

    problems = compare_non_lyrics(original, flac_parse_file(tampered), clear_fields=VORBIS_LYRICS_FIELDS)
    assert problems, "校验器未能检出非歌词内容被改动"
    assert any("TITLE" in p or "注释" in p for p in problems)


# ---------------------------------------------------------------------------
# 只清理
# ---------------------------------------------------------------------------


def test_lyrics为None时只清理歌词(flac_factory, tmp_path: Path):
    """清理无时间轴歌词时的语义：只删白名单内的歌词项，不写新歌词。"""
    src = flac_factory(
        tmp_path / "in.flac",
        entries=[
            ("TITLE", "某歌"),
            ("LYRICIST", "某作词人"),
            ("LYRICS", "纯文本歌词"),
            ("UNSYNCEDLYRICS", "另一个纯文本"),
        ],
    )
    target = tmp_path / "out.flac"
    original = flac_parse_file(src)

    flac_rewrite_with_lyrics(src, target, lyrics=None)

    comments = flac_parse_file(target).comments()
    assert comments is not None
    assert not comments.has("LYRICS")
    assert not comments.has("UNSYNCEDLYRICS")
    # 作词人必须原样保留——它不是歌词
    assert comments.has("LYRICIST")
    assert comments.get("LYRICIST") == ["某作词人".encode()]
    assert comments.get("TITLE") == ["某歌".encode()]
    assert compare_non_lyrics(original, flac_parse_file(target),
                              clear_fields=VORBIS_LYRICS_FIELDS) == []


def test_清理不存在的歌词是安全的(real_flac: Path, tmp_path: Path):
    """幂等的清理：素材本来就没有歌词时不应报错，也不应产生差异。"""
    target = tmp_path / "out.flac"
    original = flac_parse_file(real_flac)
    flac_rewrite_with_lyrics(real_flac, target, lyrics=None)
    assert compare_non_lyrics(original, flac_parse_file(target),
                              clear_fields=VORBIS_LYRICS_FIELDS) == []


def test_缺少vorbis_comment时会插入一个(flac_factory, tmp_path: Path):
    """没有 comment 块的文件也必须能写歌词。"""

    from conftest import build_streaminfo

    blocks = [(BlockType.STREAMINFO, build_streaminfo())]
    data = bytearray(FLAC_MAGIC)
    for i, (btype, payload) in enumerate(blocks):
        data.append((0x80 if i == len(blocks) - 1 else 0) | btype)
        data += len(payload).to_bytes(3, "big") + payload
    data += b"\xff" * 128
    src = tmp_path / "noc.flac"
    src.write_bytes(bytes(data))

    assert flac_parse_file(src).comments() is None

    target = tmp_path / "out.flac"
    flac_rewrite_with_lyrics(src, target, lyrics=LYRICS)
    comments = flac_parse_file(target).comments()
    assert comments is not None
    assert comments.has("LYRICS")
    assert flac_audio_hash(target) == flac_audio_hash(src), "音频仍须逐字节不变"


# ---------------------------------------------------------------------------
# 字段白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["LYRICS", "lyrics", "Lyrics", " lyrics ", "UNSYNCEDLYRICS"])
def test_白名单内的歌词字段被接受(name: str):
    assert lyrics_field_key(name) in VORBIS_LYRICS_FIELDS


@pytest.mark.parametrize("name", ["LYRICIST", "TEXT", "COMPOSER", "TCOM", "TITLE", ""])
def test_白名单外的字段被拒绝(name: str):
    """禁止用子串匹配判断歌词字段：LYRICIST（作词人）绝不能被当成歌词删除。"""
    with pytest.raises(ValueError):
        lyrics_field_key(name)


# ---------------------------------------------------------------------------
# 非法结构必须明确拒绝
# ---------------------------------------------------------------------------


def test_非FLACmagic被拒绝(tmp_path: Path):
    path = tmp_path / "fake.flac"
    path.write_bytes(b"OggS" + b"\x00" * 100)
    with pytest.raises(FlacFormatError) as excinfo:
        flac_parse_file(path)
    assert "不是 FLAC" in str(excinfo.value)


def test_缺少STREAMINFO被拒绝(tmp_path: Path):
    """结构不符规范就必须拒绝，不能猜着改。"""
    from conftest import build_vorbis_comment

    payload = build_vorbis_comment(entries=[("TITLE", "x")])
    data = bytearray(FLAC_MAGIC)
    data.append(0x80 | BlockType.VORBIS_COMMENT)  # last + 类型 4
    data += len(payload).to_bytes(3, "big") + payload
    data += b"\xff" * 64
    path = tmp_path / "nostreaminfo.flac"
    path.write_bytes(bytes(data))

    with pytest.raises(FlacFormatError) as excinfo:
        flac_parse_file(path)
    assert "STREAMINFO" in str(excinfo.value)


def test_STREAMINFO长度异常被拒绝(tmp_path: Path):
    data = bytearray(FLAC_MAGIC)
    data.append(0x80 | BlockType.STREAMINFO)
    bad = b"\x00" * 10  # 规范长度必须是 34
    data += len(bad).to_bytes(3, "big") + bad
    data += b"\xff" * 64
    path = tmp_path / "badstreaminfo.flac"
    path.write_bytes(bytes(data))

    with pytest.raises(FlacFormatError) as excinfo:
        flac_parse_file(path)
    assert "STREAMINFO 长度异常" in str(excinfo.value)


def test_非法块类型127被拒绝(tmp_path: Path):
    """127 是规范禁止的块类型；遇到必须拒绝而不是继续解析。"""
    from conftest import build_streaminfo

    data = bytearray(FLAC_MAGIC)
    si = build_streaminfo()
    data.append(BlockType.STREAMINFO)
    data += len(si).to_bytes(3, "big") + si
    data.append(0x80 | 127)  # last + 非法类型
    data += (0).to_bytes(3, "big")
    data += b"\xff" * 64
    path = tmp_path / "badblock.flac"
    path.write_bytes(bytes(data))

    with pytest.raises(FlacFormatError) as excinfo:
        flac_parse_file(path)
    assert "127" in str(excinfo.value)


def test_元数据块头截断被拒绝(tmp_path: Path):
    path = tmp_path / "truncated.flac"
    path.write_bytes(FLAC_MAGIC + b"\x00\x00")
    with pytest.raises(FlacFormatError):
        flac_parse_file(path)


def test_没有音频帧被拒绝(flac_factory, tmp_path: Path):
    """只有元数据没有音频帧不是可处理的音乐文件。"""
    from conftest import build_streaminfo

    data = bytearray(FLAC_MAGIC)
    si = build_streaminfo()
    data.append(0x80 | BlockType.STREAMINFO)
    data += len(si).to_bytes(3, "big") + si
    path = tmp_path / "noaudio.flac"
    path.write_bytes(bytes(data))

    with pytest.raises(FlacFormatError) as excinfo:
        flac_parse_file(path)
    assert "音频帧" in str(excinfo.value)


def test_vorbis_comment载荷截断被拒绝(tmp_path: Path):
    """损坏的 comment 块不能让解析产生越界读取或静默截断。"""
    from conftest import build_streaminfo

    data = bytearray(FLAC_MAGIC)
    si = build_streaminfo()
    data.append(BlockType.STREAMINFO)
    data += len(si).to_bytes(3, "big") + si
    bad = (10).to_bytes(4, "little") + b"abc"  # 声称 vendor 10 字节，实际只有 3
    data.append(0x80 | BlockType.VORBIS_COMMENT)
    data += len(bad).to_bytes(3, "big") + bad
    data += b"\xff" * 64
    path = tmp_path / "badcomment.flac"
    path.write_bytes(bytes(data))

    # 块头本身是合法的，所以文件级解析会通过；损坏在 Vorbis comment 载荷内部，
    # 必须等到真正解析注释时才被发现并拒绝，而不是产生越界读取或静默截断。
    layout = flac_parse_file(path)
    with pytest.raises(FlacFormatError) as excinfo:
        layout.comments()
    assert "截断" in str(excinfo.value)


def test_解析bytes入口(tmp_path: Path):
    """从内存解析用于测试与诊断；行为必须与文件入口一致。"""
    from conftest import build_streaminfo, build_vorbis_comment

    data = bytearray(FLAC_MAGIC)
    blocks = [
        (BlockType.STREAMINFO, build_streaminfo()),
        (BlockType.VORBIS_COMMENT, build_vorbis_comment(entries=[("TITLE", "x")])),
    ]
    for i, (btype, payload) in enumerate(blocks):
        data.append((0x80 if i == len(blocks) - 1 else 0) | btype)
        data += len(payload).to_bytes(3, "big") + payload
    data += b"\xff" * 32

    layout = flac_parse_bytes(bytes(data))
    assert layout.audio_offset == len(data) - 32
    assert layout.comments().get("TITLE") == [b"x"]


# ---------------------------------------------------------------------------
# ID3 前置：依赖文件布局的结构，必须拒绝
# ---------------------------------------------------------------------------


def test_检测ID3前置(flac_factory, tmp_path: Path):
    """一些老工具会在 FLAC 前面塞 ID3；它依赖文件布局，改写会破坏偏移。"""
    plain = flac_factory(tmp_path / "plain.flac")
    assert has_id3_prefix(plain) is False

    with_id3 = tmp_path / "withid3.flac"
    with_id3.write_bytes(b"ID3\x03\x00\x00" + plain.read_bytes())
    assert has_id3_prefix(with_id3) is True


def test_不存在的文件不抛异常(tmp_path: Path):
    assert has_id3_prefix(tmp_path / "nope.flac") is False


# ---------------------------------------------------------------------------
# VorbisComments 结构操作
# ---------------------------------------------------------------------------


def test_vorbis_comments往返序列化保持一致():
    """to_bytes/from_bytes 必须无损：它是「逐条照抄」承诺的基础。"""
    original = VorbisComments(
        vendor=b"vendor-x",
        entries=((b"A", b"A=1"), (b"A", b"A=2"), (b"B", b"B=3")),
    )
    restored = VorbisComments.from_bytes(original.to_bytes())
    assert restored.vendor == original.vendor
    assert restored.entries == original.entries


def test_vorbis_comments保留重复项顺序():
    comments = VorbisComments(
        vendor=b"v", entries=((b"A", b"A=1"), (b"B", b"B=1"), (b"A", b"A=2"))
    )
    assert comments.get("A") == [b"1", b"2"]
    assert comments.has("A") is True
    assert comments.has("C") is False
    # 删除 A 后 B 的相对位置语义仍由剩余序列体现
    assert [n for n, _ in comments.without(["A"]).entries] == [b"B"]


def test_vorbis_comments追加到末尾():
    comments = VorbisComments(vendor=b"v", entries=((b"A", b"A=1"),))
    grown = comments.with_entry("LYRICS", "[00:01.00]x")
    assert [n for n, _ in grown.entries] == [b"A", b"LYRICS"]
    assert grown.get("LYRICS") == [b"[00:01.00]x"]


def test_vorbis_comments项数异常被拒绝():
    """损坏文件里的天文数字项数不能导致长时间循环。"""
    payload = (0).to_bytes(4, "little") + (2_000_000).to_bytes(4, "little")
    with pytest.raises(FlacFormatError) as excinfo:
        VorbisComments.from_bytes(payload)
    assert "项数异常" in str(excinfo.value)


def test_无等号的注释项不产生值():
    """`KEY` 这种没有 `=` 的畸形项不应被当作有值。"""
    payload = (0).to_bytes(4, "little") + (1).to_bytes(4, "little")
    payload += (3).to_bytes(4, "little") + b"ABC"
    comments = VorbisComments.from_bytes(payload)
    assert comments.entries[0][0] == b"ABC"
    assert comments.get("ABC") == []
    assert comments.has("ABC") is True
