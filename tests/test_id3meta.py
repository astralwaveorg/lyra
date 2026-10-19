"""`id3meta` 的离线测试。

## 测试素材的来源（真实、可复现）

`tests/fixtures/audio/sample_v23.mp3` 与 `sample_v24.mp3` 是 NAS 上
ffmpeg 8.1.1 的真实产物（命令见 `FIXTURE_PROVENANCE`），分别带 ID3v2.3 与
ID3v2.4 标签。测试时用 **mutagen**（与被测实现不同的另一个实现）在其上追加
`TEXT`（作词人）、`APIC`（封面）、`COMM`、`TXXX` 与 ID3v1 / APEv2，
以及构造几种「必须拒绝」的畸形标签。

用外部实现造素材可以避免「自己造、自己验」的循环论证。

## 覆盖的关键断言

- 主版本绝不升级（v2.3 进 → v2.3 出）
- 音频哈希、非歌词帧原始字节、尾部标签完全不变
- `TEXT`（作词人）不是歌词，必须原样保留
- 全局 unsynchronisation / 扩展头 CRC / 压缩帧 → 明确拒绝
"""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import pytest
from mutagen.apev2 import APEv2
from mutagen.id3 import APIC, COMM, ID3, TEXT, TXXX, USLT
from mutagen.mp3 import MP3

from lyra import id3meta
from lyra.formats import ID3_LYRICS_FRAMES

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "audio"

#: 素材是怎么来的（实际执行的命令，本地侧 ffmpeg 8.1.1）
FIXTURE_PROVENANCE = (
    "ffmpeg -f lavfi -i sine=frequency=440:duration=2 -c:a libmp3lame -b:a 128k "
    "-id3v2_version 3 -metadata title=... -y sample_v23.mp3",
    "ffmpeg -f lavfi -i sine=frequency=440:duration=2 -c:a libmp3lame -b:a 128k "
    "-id3v2_version 4 -metadata title=... -y sample_v24.mp3",
)

LYRICS = "[00:01.00]第一行歌词\n[00:02.50]第二行歌词\n[00:03.75]第三行\n"
OLD_PLAIN_LYRICS = "[00:01.00]旧歌词\n"

_BASE_V23 = FIXTURE_DIR / "sample_v23.mp3"
_BASE_V24 = FIXTURE_DIR / "sample_v24.mp3"


# ---------------------------------------------------------------------------
# 素材构造工具
# ---------------------------------------------------------------------------


def _synchsafe(value: int) -> bytes:
    return bytes(
        ((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F)
    )


def _png_1x1() -> bytes:
    """一张真正合法的 1×1 PNG，用作 APIC 封面。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="session")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("id3meta")


@pytest.fixture(scope="session")
def mp3_v23(workdir: Path) -> Path:
    """ID3v2.3 + 作词人 TEXT + APIC 封面 + COMM/TXXX + 无时间轴 USLT + ID3v1。"""
    target = workdir / "tagged_v23.mp3"
    shutil.copy(_BASE_V23, target)
    tags = ID3(str(target))
    tags.add(TEXT(encoding=3, text="作词人甲"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=_png_1x1()))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="一段备注"))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text="abc-123"))
    tags.add(USLT(encoding=3, lang="eng", desc="", text=OLD_PLAIN_LYRICS))
    tags.update_to_v23()  # mutagen 文档要求：写 v2.3 前先做版本降级转换
    tags.save(str(target), v1=2, v2_version=3)  # v1=2 才会真正创建 ID3v1
    return target


@pytest.fixture(scope="session")
def mp3_v24(workdir: Path) -> Path:
    """ID3v2.4 + TEXT + APIC + COMM/TXXX，不带 ID3v1。"""
    target = workdir / "tagged_v24.mp3"
    shutil.copy(_BASE_V24, target)
    tags = ID3(str(target))
    tags.add(TEXT(encoding=3, text="作词人甲"))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=_png_1x1()))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="一段备注"))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text="abc-123"))
    tags.save(str(target), v1=0, v2_version=4)
    return target


@pytest.fixture(scope="session")
def mp3_ape(workdir: Path) -> Path:
    """ID3v2.3 + 尾部 APEv2，用于验证尾部标签不被破坏。"""
    target = workdir / "tagged_ape.mp3"
    shutil.copy(_BASE_V23, target)
    ape = APEv2()
    ape["Foo"] = "Bar"
    ape.save(str(target))
    return target


@pytest.fixture(scope="session")
def mp3_global_unsync(workdir: Path) -> Path:
    """构造一个**真正**全局 unsynchronisation 的 ID3v2 标签。

    对标签体施加字节填充（0xFF 后接 0x00 或 0xE0~0xFF 时插入 0x00），
    置上标志位并更新长度——不是只翻一个标志位。
    """
    target = workdir / "global_unsync.mp3"
    raw = _BASE_V23.read_bytes()
    layout = id3meta.id3_parse_file(_BASE_V23)
    body = raw[10 : layout.tag_size]

    stuffed = bytearray()
    for index, byte in enumerate(body):
        stuffed.append(byte)
        following = body[index + 1] if index + 1 < len(body) else None
        if byte == 0xFF and (following is None or following == 0x00 or following >= 0xE0):
            stuffed.append(0x00)

    header = (
        b"ID3"
        + bytes((layout.version, layout.revision, layout.flags | 0x80))
        + _synchsafe(len(stuffed))
    )
    target.write_bytes(header + bytes(stuffed) + raw[layout.tag_size :])
    return target


@pytest.fixture(scope="session")
def mp3_extended_header(workdir: Path) -> Path:
    """构造一个带 CRC 的 v2.3 扩展头。"""
    target = workdir / "extended_header.mp3"
    raw = _BASE_V23.read_bytes()
    layout = id3meta.id3_parse_file(_BASE_V23)
    body = raw[10 : layout.tag_size]

    # v2.3 扩展头：大小(4)=10 + 标志(2)=0x8000(含 CRC) + 填充大小(4) + CRC(4)
    extended = struct.pack(">IHI", 10, 0x8000, 0) + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    new_body = extended + body
    header = (
        b"ID3"
        + bytes((layout.version, layout.revision, layout.flags | 0x40))
        + _synchsafe(len(new_body))
    )
    target.write_bytes(header + new_body + raw[layout.tag_size :])
    return target


@pytest.fixture(scope="session")
def mp3_compressed_frame(workdir: Path) -> Path:
    """把 TIT2 帧真正 zlib 压缩，并按 v2.3 规矩加上解压后长度前缀与压缩标志。"""
    target = workdir / "compressed_frame.mp3"
    raw = _BASE_V23.read_bytes()
    layout = id3meta.id3_parse_file(_BASE_V23)

    rebuilt = bytearray()
    for frame in layout.frames:
        if frame.frame_id != "TIT2":
            rebuilt += frame.raw
            continue
        payload = frame.body
        compressed = struct.pack(">I", len(payload)) + zlib.compress(payload)
        rebuilt += (
            frame.frame_id.encode("ascii")
            + len(compressed).to_bytes(4, "big")
            + bytes((frame.raw[8], frame.raw[9] | 0x80))  # 压缩标志
            + compressed
        )

    new_body = bytes(rebuilt) + b"\x00" * layout.padding_size
    header = (
        b"ID3"
        + bytes((layout.version, layout.revision, layout.flags))
        + _synchsafe(len(new_body))
    )
    target.write_bytes(header + new_body + raw[layout.audio_offset :])
    return target


@pytest.fixture(scope="session")
def mp3_without_id3v2(workdir: Path) -> Path:
    """剥掉 ID3v2 标签的裸 MP3（只剩音频，可能带 ID3v1）。"""
    target = workdir / "no_id3v2.mp3"
    raw = _BASE_V23.read_bytes()
    layout = id3meta.id3_parse_file(_BASE_V23)
    target.write_bytes(raw[layout.tag_size :])
    return target


def _non_lyrics(layout: id3meta.Id3Layout) -> list[tuple[str, bytes]]:
    drop = {frame_id.upper() for frame_id in ID3_LYRICS_FRAMES}
    return [(f.frame_id, f.raw) for f in layout.frames if f.frame_id.upper() not in drop]


def _frame(layout: id3meta.Id3Layout, frame_id: str) -> id3meta.Id3Frame | None:
    for frame in layout.frames:
        if frame.frame_id == frame_id:
            return frame
    return None


# ---------------------------------------------------------------------------
# 主版本
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "expected"), [("mp3_v23", 3), ("mp3_v24", 4)])
def test_major_version_is_never_changed(
    fixture: str, expected: int, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """v2.3 进去必须 v2.3 出来，v2.4 同理——不允许悄悄升级版本。

    v2.3 与 v2.4 的帧长度字段语义不同（普通大端 vs synchsafe），
    悄悄改版本会直接写坏文件。
    """
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    before = id3meta.id3_parse_file(source)

    id3meta.id3_rewrite_with_lyrics(source, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)

    assert before.version == expected
    assert after.version == expected
    assert after.revision == before.revision
    # 直接看文件头的版本字节，避免只信任自己的解析器
    assert target.read_bytes()[3] == expected


# ---------------------------------------------------------------------------
# 数据保持
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["mp3_v23", "mp3_v24"])
def test_audio_hash_and_non_lyrics_unchanged(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    before = id3meta.id3_parse_file(source)

    id3meta.id3_rewrite_with_lyrics(source, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)

    # 1) MPEG 音频数据段逐字节不变
    assert after.audio_hash() == before.audio_hash()
    assert id3meta.id3_audio_hash(target) == id3meta.id3_audio_hash(source)

    # 2) 除歌词帧外逐帧原始字节一致（含顺序）
    assert id3meta.compare_non_lyrics(before, after, clear_fields=ID3_LYRICS_FRAMES) == []
    assert _non_lyrics(after) == _non_lyrics(before)

    # 3) 歌词确实写进去了
    uslt = _frame(after, "USLT")
    assert uslt is not None


def test_text_lyricist_frame_is_preserved_byte_for_byte(
    mp3_v23: Path, tmp_path: Path
) -> None:
    """`TEXT` 是作词人，不是歌词；必须原样保留。"""
    target = tmp_path / "out.mp3"
    before = id3meta.id3_parse_file(mp3_v23)
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)

    text_before = _frame(before, "TEXT")
    text_after = _frame(after, "TEXT")
    assert text_before is not None, "素材里应当有作词人帧"
    assert text_after is not None, "作词人帧被误删了"
    assert text_after.raw == text_before.raw
    # 用 mutagen 独立确认内容仍在
    assert ID3(str(target)).getall("TEXT")


def test_apic_cover_is_preserved_byte_for_byte(mp3_v23: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.mp3"
    before = id3meta.id3_parse_file(mp3_v23)
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)

    apic_before = _frame(before, "APIC")
    apic_after = _frame(after, "APIC")
    assert apic_before is not None
    assert apic_after is not None
    assert apic_after.raw == apic_before.raw

    # 用 mutagen 独立确认封面字节完好
    cover = ID3(str(target)).getall("APIC")
    assert len(cover) == 1
    assert cover[0].data == _png_1x1()


def test_id3v1_trailer_is_preserved(mp3_v23: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.mp3"
    before = id3meta.id3_parse_file(mp3_v23)
    assert len(before.trailer) == id3meta.ID3V1_SIZE, "素材应当带 ID3v1 尾部"
    assert before.trailer[:3] == b"TAG"

    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)
    assert after.trailer == before.trailer
    assert target.read_bytes()[-id3meta.ID3V1_SIZE :][:3] == b"TAG"


def test_apev2_trailer_is_preserved(mp3_ape: Path, tmp_path: Path) -> None:
    target = tmp_path / "out.mp3"
    before = id3meta.id3_parse_file(mp3_ape)
    assert before.trailer[-32:-24] == b"APETAGEX", "素材应当带 APEv2 尾部"

    id3meta.id3_rewrite_with_lyrics(mp3_ape, target, lyrics=LYRICS)
    after = id3meta.id3_parse_file(target)

    assert after.trailer == before.trailer
    assert id3meta.compare_non_lyrics(before, after, clear_fields=ID3_LYRICS_FRAMES) == []
    # APEv2 头部/尾部不能被算进音频哈希
    assert after.audio_hash() == before.audio_hash()
    # 与没有尾部标签的原始文件相比，音频哈希必须相同
    assert id3meta.id3_audio_hash(mp3_ape) == id3meta.id3_audio_hash(_BASE_V23)
    assert APEv2(str(target))["Foo"] == "Bar"


def test_tag_size_stays_constant_when_lyrics_fit(mp3_v23: Path, tmp_path: Path) -> None:
    """新歌词装得下时，通过收缩填充区保持标签（乃至整个文件）长度不变。"""
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    assert target.stat().st_size == mp3_v23.stat().st_size
    assert id3meta.id3_parse_file(target).tag_size == id3meta.id3_parse_file(mp3_v23).tag_size


def test_tag_grows_when_lyrics_do_not_fit(mp3_v23: Path, tmp_path: Path) -> None:
    """歌词远大于原标签空间时必须增长标签，而不是截断。"""
    source = tmp_path / "source.mp3"
    shutil.copy(mp3_v23, source)
    # 先把标签缩到很小，制造「装不下」的情形
    tags = ID3(str(source))
    tags.save(str(source), v1=2, v2_version=3, padding=lambda _info: 0)

    huge = "\n".join(f"[{i // 60:02d}:{i % 60:02d}.00]第 {i} 行" for i in range(400))
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(source, target, lyrics=huge)

    after = id3meta.id3_parse_file(target)
    assert _frame(after, "USLT") is not None
    assert ID3(str(target)).getall("USLT")[0].text == huge
    # 音频仍然完好
    assert after.audio_hash() == id3meta.id3_parse_file(source).audio_hash()


# ---------------------------------------------------------------------------
# 写入 / 清理语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "expected_encoding"), [("mp3_v23", 0x01), ("mp3_v24", 0x03)])
def test_lyrics_encoding_matches_id3_version(
    fixture: str, expected_encoding: int, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """v2.4 用 UTF-8；**v2.3 规范里没有 UTF-8**，只能写带 BOM 的 UTF-16。"""
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    id3meta.id3_rewrite_with_lyrics(source, target, lyrics=LYRICS)
    uslt = _frame(id3meta.id3_parse_file(target), "USLT")
    assert uslt is not None
    assert uslt.body[0] == expected_encoding


@pytest.mark.parametrize("fixture", ["mp3_v23", "mp3_v24"])
def test_mutagen_reads_back_lyrics_and_tags(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """用 mutagen 独立读回：歌词正确、其它帧与封面完好、时长不变。"""
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    id3meta.id3_rewrite_with_lyrics(source, target, lyrics=LYRICS)

    tags = ID3(str(target))
    uslt = tags.getall("USLT")
    assert len(uslt) == 1
    assert uslt[0].text == LYRICS

    assert tags.getall("TEXT")[0].text == ["作词人甲"]
    assert tags.getall("COMM")[0].text == ["一段备注"]
    assert tags.getall("TXXX")[0].text == ["abc-123"]
    cover = tags.getall("APIC")
    assert len(cover) == 1 and cover[0].data == _png_1x1()

    assert MP3(str(target)).info.length == pytest.approx(
        MP3(str(source)).info.length, abs=1e-6
    )


def test_existing_language_and_descriptor_are_reused(mp3_v23: Path, tmp_path: Path) -> None:
    """沿用原 USLT 的语言与描述符：有些播放器以它们为歌词条目的键。"""
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    frame = _frame(id3meta.id3_parse_file(target), "USLT")
    assert frame is not None
    assert frame.body[1:4] == b"eng"


def test_lyrics_none_only_clears(mp3_v23: Path, tmp_path: Path) -> None:
    """lyrics=None 时只清理歌词帧，不写入新歌词。"""
    before = id3meta.id3_parse_file(mp3_v23)
    assert _frame(before, "USLT") is not None, "素材应当带一个无时间轴 USLT"

    target = tmp_path / "cleared.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=None)
    after = id3meta.id3_parse_file(target)

    assert after.lyrics_frames() == []
    assert ID3(str(target)).getall("USLT") == []
    # 非歌词帧与音频不受影响
    assert _non_lyrics(after) == _non_lyrics(before)
    assert after.audio_hash() == before.audio_hash()
    # 作词人必须还在
    assert _frame(after, "TEXT") is not None


def test_clear_then_write_removes_old_lyrics(mp3_v23: Path, tmp_path: Path) -> None:
    """写入新歌词时，旧的歌词帧被替换而不是叠加。"""
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    frames = id3meta.id3_parse_file(target).lyrics_frames()
    assert [f.frame_id for f in frames] == ["USLT"]
    assert OLD_PLAIN_LYRICS not in frames[0].body.decode("utf-16", "ignore")


def test_repeated_write_is_idempotent(mp3_v23: Path, tmp_path: Path) -> None:
    first = tmp_path / "a.mp3"
    second = tmp_path / "b.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, first, lyrics=LYRICS)
    id3meta.id3_rewrite_with_lyrics(first, second, lyrics=LYRICS)
    assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------


def test_non_whitelisted_clear_field_is_rejected(mp3_v23: Path, tmp_path: Path) -> None:
    """防呆：不允许把 TEXT（作词人）传进 clear_fields。"""
    with pytest.raises(ValueError, match="白名单"):
        id3meta.id3_rewrite_with_lyrics(
            mp3_v23, tmp_path / "out.mp3", lyrics=LYRICS, clear_fields=("TEXT",)
        )


def test_text_is_not_in_the_lyrics_whitelist() -> None:
    assert "TEXT" not in ID3_LYRICS_FRAMES
    assert set(ID3_LYRICS_FRAMES) == {"USLT", "SYLT"}


# ---------------------------------------------------------------------------
# 明确拒绝
# ---------------------------------------------------------------------------


def test_global_unsynchronisation_is_rejected(
    mp3_global_unsync: Path, tmp_path: Path
) -> None:
    """全局 unsynchronisation：标签字节与实际内容不同，无法安全改写。"""
    # 先确认素材确实带了标志位
    assert mp3_global_unsync.read_bytes()[5] & 0x80
    with pytest.raises(id3meta.Id3FormatError, match="unsynchronisation"):
        id3meta.id3_rewrite_with_lyrics(
            mp3_global_unsync, tmp_path / "out.mp3", lyrics=LYRICS
        )


def test_extended_header_is_rejected(mp3_extended_header: Path, tmp_path: Path) -> None:
    """扩展头可能带 CRC，CRC 依赖整个标签的字节布局。"""
    assert mp3_extended_header.read_bytes()[5] & 0x40
    with pytest.raises(id3meta.Id3FormatError, match="扩展头"):
        id3meta.id3_rewrite_with_lyrics(
            mp3_extended_header, tmp_path / "out.mp3", lyrics=LYRICS
        )


def test_compressed_frame_is_rejected(mp3_compressed_frame: Path, tmp_path: Path) -> None:
    """压缩帧的内容不可直接解释，拒绝而不是通用重写。"""
    with pytest.raises(id3meta.Id3FormatError, match="压缩"):
        id3meta.id3_rewrite_with_lyrics(
            mp3_compressed_frame, tmp_path / "out.mp3", lyrics=LYRICS
        )


def test_missing_id3v2_is_rejected(mp3_without_id3v2: Path, tmp_path: Path) -> None:
    """没有 ID3v2 就没有「原有主版本」可保持，拒绝自行选择版本创建。"""
    with pytest.raises(id3meta.Id3FormatError, match="没有 ID3v2"):
        id3meta.id3_rewrite_with_lyrics(
            mp3_without_id3v2, tmp_path / "out.mp3", lyrics=LYRICS
        )


def test_rejection_leaves_no_partial_output(
    mp3_global_unsync: Path, tmp_path: Path
) -> None:
    """拒绝必须是「什么都没写」，不能留下半截文件。"""
    target = tmp_path / "should_not_exist.mp3"
    with pytest.raises(id3meta.Id3FormatError):
        id3meta.id3_rewrite_with_lyrics(mp3_global_unsync, target, lyrics=LYRICS)
    assert not target.exists()


def test_non_mp3_input_rejected() -> None:
    with pytest.raises(id3meta.Id3FormatError):
        id3meta.id3_parse_file(FIXTURE_DIR / "sample_vorbis.ogg")


# ---------------------------------------------------------------------------
# compare_non_lyrics 的判别力
# ---------------------------------------------------------------------------


def test_compare_non_lyrics_detects_tampering(mp3_v23: Path, tmp_path: Path) -> None:
    """证明 compare_non_lyrics 不是永远返回空列表。"""
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)

    before = id3meta.id3_parse_file(mp3_v23)
    after = id3meta.id3_parse_file(target)

    # 把「清空歌词」的范围设成空集合：原文件多出的 USLT 帧就会被视为差异
    problems = id3meta.compare_non_lyrics(before, after, clear_fields=())
    assert problems, "歌词帧差异必须被检出"
    assert any("非歌词帧" in problem or "数量" in problem for problem in problems)


def test_compare_non_lyrics_detects_audio_change(mp3_v23: Path, tmp_path: Path) -> None:
    """音频被动过必须被检出。"""
    target = tmp_path / "out.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)

    before = id3meta.id3_parse_file(mp3_v23)
    after = id3meta.id3_parse_file(target)

    tampered = id3meta.Id3Layout(
        version=after.version,
        revision=after.revision,
        flags=after.flags,
        frames=after.frames,
        padding_size=after.padding_size,
        tag_size=after.tag_size,
        audio_offset=after.audio_offset,
        audio_end=after.audio_end,
        audio_sha256="0" * 64,
        trailer=after.trailer,
        file_size=after.file_size,
    )
    problems = id3meta.compare_non_lyrics(before, tampered, clear_fields=ID3_LYRICS_FRAMES)
    assert any("音频" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 真实解码
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机未安装 ffmpeg")
def test_ffmpeg_decodes_rewritten_mp3(mp3_v23: Path, tmp_path: Path) -> None:
    """真实解码验证。

    本机（macOS 开发机）未安装 ffmpeg，因此这里通常是 **skip**——跳过不等于通过。
    同一验证在 NAS（装有 ffmpeg 8.1.1）上以等价命令执行，结果记录在交付说明中。
    """
    import subprocess

    target = tmp_path / "decoded.mp3"
    id3meta.id3_rewrite_with_lyrics(mp3_v23, target, lyrics=LYRICS)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(target), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "", f"ffmpeg 报告了错误: {result.stderr}"


def test_rewrite_generic_tags_preserves_audio_and_other_frames(
    mp3_v24: Path, tmp_path: Path
) -> None:
    from lyra.formats import read_all_tags

    target = tmp_path / "tags.mp3"
    before = id3meta.id3_parse_file(mp3_v24)
    id3meta.id3_rewrite_tags(
        mp3_v24,
        target,
        tags={
            "TITLE": ["新标题"],
            "ARTIST": ["新艺人"],
            "ARTISTS": ["新艺人", "合作者"],
            "ALBUM": ["新专辑"],
            "DATE": ["2024-05-06"],
            "TRACKNUMBER": ["3"],
            "TOTALTRACKS": ["12"],
            "MUSICBRAINZ_ALBUMID": ["new-album-id"],
            "MUSICBRAINZ_ARTISTID": ["id-1", "id-2"],
            "CATALOGNUMBER": ["CAT-9"],
        },
    )
    after = id3meta.id3_parse_file(target)
    assert after.audio_hash() == before.audio_hash()

    # 封面、歌词、作词人、备注必须逐字节保留
    for frame_id in ("APIC", "USLT", "TEXT", "COMM"):
        old = [f.raw for f in before.frames if f.frame_id == frame_id]
        new = [f.raw for f in after.frames if f.frame_id == frame_id]
        assert new == old, frame_id

    tags, error = read_all_tags(target)
    assert error is None
    assert tags["TITLE"] == ["新标题"]
    assert tags["ARTIST"] == ["新艺人"]
    assert tags["ARTISTS"] == ["新艺人", "合作者"]
    assert tags["ALBUM"] == ["新专辑"]
    assert tags["DATE"] == ["2024-05-06"]
    assert tags["TRACKNUMBER"] == ["3"]
    assert tags["TRACKTOTAL"] == ["12"]
    assert tags["TOTALTRACKS"] == ["12"]
    assert tags["MUSICBRAINZ_ALBUMID"] == ["new-album-id"]
    assert tags["MUSICBRAINZ_ARTISTID"] == ["id-1", "id-2"]
    assert tags["CATALOGNUMBER"] == ["CAT-9"]


def test_apply_tag_edit_supports_mp3(mp3_v24: Path) -> None:
    from lyra.models import MediaFormat
    from lyra.writer import TagEditPlan, apply_tag_edit

    before = id3meta.id3_audio_hash(mp3_v24)
    result = apply_tag_edit(
        TagEditPlan(
            audio_path=mp3_v24,
            media_format=MediaFormat.MP3,
            tags={
                "TITLE": ["MP3 标签"],
                "ARTIST": ["测试艺人"],
                "ALBUM": ["测试专辑"],
                "TRACKNUMBER": ["2"],
                "TOTALTRACKS": ["8"],
                "MUSICBRAINZ_TRACKID": ["rec-1"],
            },
            remove=["GENRE"],
            original_audio_hash=before,
        )
    )
    assert result.ok, result.error
    assert id3meta.id3_audio_hash(mp3_v24) == before
    from lyra.formats import read_all_tags

    tags, error = read_all_tags(mp3_v24)
    assert error is None
    assert tags["TITLE"] == ["MP3 标签"]
    assert tags["TRACKNUMBER"] == ["2"]
    assert tags["TRACKTOTAL"] == ["8"]
    assert tags["MUSICBRAINZ_TRACKID"] == ["rec-1"]


def test_rewrite_generic_tags_keeps_v23_and_id3v1(mp3_v23: Path, tmp_path: Path) -> None:
    from lyra.formats import read_all_tags

    target = tmp_path / "tags_v23.mp3"
    before = id3meta.id3_parse_file(mp3_v23)
    id3meta.id3_rewrite_tags(
        mp3_v23,
        target,
        tags={
            "TITLE": ["标题"],
            "ARTIST": ["艺人"],
            "DATE": ["2024-05-06"],
            "TRACKNUMBER": ["3"],
            "TOTALTRACKS": ["12"],
        },
    )
    after = id3meta.id3_parse_file(target)
    assert after.version == 3
    assert after.trailer == before.trailer
    assert after.audio_hash() == before.audio_hash()
    tags, error = read_all_tags(target)
    assert error is None
    assert tags["TITLE"] == ["标题"]
    assert tags["DATE"] == ["2024-05-06"]
    assert tags["TRACKNUMBER"] == ["3"]
    assert tags["TRACKTOTAL"] == ["12"]
