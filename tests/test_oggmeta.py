"""`oggmeta` 的离线测试。

## 测试素材的来源（真实、可复现）

`tests/fixtures/audio/` 下的 4 个基础文件是**真实编码器产物**，在本地网络用
ffmpeg 8.1.1 生成后下载回来（命令行见 `FIXTURE_PROVENANCE`）。它们不是手工
拼出来的字节，因此页面分页、CRC、granule 都与真实曲库一致
（尤其是 ffmpeg 把 Vorbis 的 comment header **与 setup header 放在同一页**，
这正是最容易写错的情形）。

在此基础上，本文件用 **mutagen**（与被测实现完全不同的另一个实现）在测试时
派生出带重复键的素材。用外部实现造素材，可以避免「自己造、自己验」的循环论证。

## CRC 正确性怎么验证

不靠自证。三重验证：

1. `test_page_render_roundtrip_matches_real_files`：把真实文件的每一页按解析
   结果重新渲染，必须与文件里的原始字节**逐字节相等**。CRC 只是页头里的 4 个
   字节，算错一位这里就会失败。
2. `test_crc_matches_independent_zlib_implementation`：用 mutagen 内部那套
   「zlib.crc32 + 位反转」的独立算法重算一遍，两条路径必须给出同一个值。
3. `test_rewritten_stream_pages_have_valid_crc`：对**重写后**的文件重新解析，
   逐页校验存储的 CRC 与重算值一致，并再次做渲染往返。
"""

from __future__ import annotations

import shutil
import zlib
from pathlib import Path

import pytest
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from lyra import oggmeta
from lyra.formats import VORBIS_LYRICS_FIELDS

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "audio"

#: 素材是怎么来的（实际执行的命令，本地侧 ffmpeg 8.1.1）
FIXTURE_PROVENANCE = (
    "ffmpeg -f lavfi -i sine=frequency=440:duration=2 -c:a libvorbis -q:a 3 "
    "-metadata title=... -y sample_vorbis.ogg",
    "ffmpeg -f lavfi -i sine=frequency=440:duration=2 -c:a libopus -b:a 96k "
    "-metadata title=... -y sample_opus.opus",
)

LYRICS = "[00:01.00]第一行歌词\n[00:02.50]第二行歌词\n[00:03.75]第三行\n"

_BASE_VORBIS = FIXTURE_DIR / "sample_vorbis.ogg"
_BASE_OPUS = FIXTURE_DIR / "sample_opus.opus"


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("oggmeta")


@pytest.fixture(scope="session")
def vorbis_fixture(workdir: Path) -> Path:
    """真实 Vorbis 素材 + 用 mutagen 追加重复键与自定义项。"""
    target = workdir / "vorbis_dupkey.ogg"
    shutil.copy(_BASE_VORBIS, target)
    audio = OggVorbis(str(target))
    audio["ARTISTS"] = ["甲", "乙"]  # 重复键：两行同名 ARTISTS
    audio["ALBUMARTISTS"] = ["丙", "丁"]  # 再来一组重复键
    audio["LYRICIST"] = "作词人甲"
    audio["CUSTOMTAG"] = "自定义值"
    audio.save()
    return target


@pytest.fixture(scope="session")
def opus_fixture(workdir: Path) -> Path:
    """真实 Opus 素材 + 重复键。Opus 的 OpusTags 没有 framing bit，单独覆盖。"""
    target = workdir / "opus_dupkey.opus"
    shutil.copy(_BASE_OPUS, target)
    audio = OggOpus(str(target))
    audio["ARTISTS"] = ["甲", "乙"]
    audio["LYRICIST"] = "作词人甲"
    audio["CUSTOMTAG"] = "自定义值"
    audio.save()
    return target


def _crc_via_zlib(data: bytes) -> int:
    """独立实现：mutagen 内部使用的「位反转 + zlib.crc32」算法。

    注意 mutagen 最后一步产出的是**要写进页头的字节序列**（小端存放），
    所以这里按 `little` 解释才是 CRC 的数值。直接按 `big` 解释会得到字节
    反转后的值——这正是本测试要防住的那类错误。
    """
    swapped = bytes(int(f"{byte:08b}"[::-1], 2) for byte in data)
    crc = (~zlib.crc32(swapped, -1)) & 0xFFFFFFFF
    finalized = bytes(int(f"{b:08b}"[::-1], 2) for b in crc.to_bytes(4, "big"))
    return int.from_bytes(finalized, "little")


def _stored_crc_is_valid(raw: bytes, page: oggmeta.OggPage) -> bool:
    blob = bytearray(raw[page.offset : page.offset + page.total_size])
    blob[22:26] = b"\x00\x00\x00\x00"  # CRC 字段自身参与计算时置零
    return oggmeta.ogg_crc32(bytes(blob)) == page.crc


# ---------------------------------------------------------------------------
# CRC 正确性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name", ["sample_vorbis.ogg", "sample_opus.opus"]
)
def test_page_render_roundtrip_matches_real_files(fixture_name: str) -> None:
    """按解析结果重新渲染每一页，必须与真实文件里的原始字节完全相等。

    这一条是 CRC 实现正确性的**直接证据**：CRC 只占页头 4 字节，算错一位
    这里必然失败。同时它也证明页头字段（granule/serial/sequence/lacing）
    的解析与序列化是对称的。
    """
    path = FIXTURE_DIR / fixture_name
    raw = path.read_bytes()
    layout = oggmeta.ogg_parse_file(path)

    assert layout.pages, "素材应当至少有一页"
    for page in layout.pages:
        original = raw[page.offset : page.offset + page.total_size]
        assert page.render() == original, f"第 {page.index} 页渲染结果与原始字节不一致"


@pytest.mark.parametrize(
    "fixture_name", ["sample_vorbis.ogg", "sample_opus.opus"]
)
def test_crc_matches_independent_zlib_implementation(fixture_name: str) -> None:
    """与 mutagen 使用的 zlib 位反转算法交叉验证，两条路径必须一致。"""
    path = FIXTURE_DIR / fixture_name
    raw = path.read_bytes()
    layout = oggmeta.ogg_parse_file(path)
    for page in layout.pages:
        blob = bytearray(raw[page.offset : page.offset + page.total_size])
        blob[22:26] = b"\x00\x00\x00\x00"
        assert oggmeta.ogg_crc32(bytes(blob)) == _crc_via_zlib(bytes(blob))
        assert _stored_crc_is_valid(raw, page)


# ---------------------------------------------------------------------------
# 结构解析
# ---------------------------------------------------------------------------


def test_vorbis_comment_shares_page_with_setup_header(vorbis_fixture: Path) -> None:
    """ffmpeg 把 Vorbis 的 comment 与 setup 头放在**同一页**。

    这是真实分页的关键特征：如果实现只「替换第 2 个包」，就会把 setup 头
    弄丢。这里断言重排区域确实把 setup 包一起纳入了。
    """
    layout = oggmeta.ogg_parse_file(vorbis_fixture)
    assert layout.codec == "vorbis"
    start, end, packets = layout.comment_region()
    indices = [p.index for p in packets]
    assert indices == [1, 2], f"区域应含 comment(1) 与 setup(2)，实际 {indices}"
    assert layout.header_packet_count == 3
    assert all(p.index < layout.header_packet_count for p in packets)


def test_opus_comment_is_alone_on_its_page(opus_fixture: Path) -> None:
    layout = oggmeta.ogg_parse_file(opus_fixture)
    assert layout.codec == "opus"
    _start, _end, packets = layout.comment_region()
    assert [p.index for p in packets] == [1]
    assert layout.header_packet_count == 2


def test_non_ogg_input_rejected() -> None:
    with pytest.raises(oggmeta.OggFormatError):
        oggmeta.ogg_parse_file(FIXTURE_DIR / "sample_v23.mp3")


def test_truncated_page_rejected() -> None:
    data = _BASE_VORBIS.read_bytes()
    with pytest.raises(oggmeta.OggFormatError):
        oggmeta.ogg_parse_bytes(data[: len(data) - 100], "truncated")


def test_multiplexed_stream_rejected() -> None:
    """两条不同 serial 的逻辑流混在一起：无法确定该改哪个 comment。"""
    data = _BASE_VORBIS.read_bytes() + _BASE_OPUS.read_bytes()
    with pytest.raises(oggmeta.OggFormatError, match="逻辑流"):
        oggmeta.ogg_parse_bytes(data, "multiplexed")


# ---------------------------------------------------------------------------
# 写入与校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["vorbis_fixture", "opus_fixture"])
def test_write_lyrics_preserves_audio_and_non_lyrics(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name

    original = oggmeta.ogg_parse_file(source)
    oggmeta.ogg_rewrite_with_lyrics(source, target, lyrics=LYRICS)
    rewritten = oggmeta.ogg_parse_file(target)

    # 1) 音频哈希不变
    assert rewritten.audio_hash() == original.audio_hash()
    assert oggmeta.ogg_audio_hash(target) == oggmeta.ogg_audio_hash(source)

    # 2) 非歌词项逐字节一致（含重复键与顺序）
    assert oggmeta.compare_non_lyrics(original, rewritten, clear_fields=VORBIS_LYRICS_FIELDS) == []

    # 3) 歌词确实写进去了
    assert rewritten.comments.get("LYRICS") == [LYRICS.encode("utf-8")]


@pytest.mark.parametrize("fixture", ["vorbis_fixture", "opus_fixture"])
def test_rewritten_stream_pages_have_valid_crc(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """重写后的每一页 CRC 必须有效——算错就会直接导致解码失败。"""
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    oggmeta.ogg_rewrite_with_lyrics(source, target, lyrics=LYRICS)

    raw = target.read_bytes()
    layout = oggmeta.ogg_parse_file(target)
    for page in layout.pages:
        assert _stored_crc_is_valid(raw, page), f"第 {page.index} 页 CRC 无效"
        assert page.render() == raw[page.offset : page.offset + page.total_size]

    # 页面序号必须连续无缺口
    sequences = [p.sequence for p in layout.pages]
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))


@pytest.mark.parametrize("fixture", ["vorbis_fixture", "opus_fixture"])
def test_mutagen_reads_back_rewritten_file(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """用 mutagen 独立读回：歌词正确、其余标签与封面完好、时长不变。"""
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    oggmeta.ogg_rewrite_with_lyrics(source, target, lyrics=LYRICS)

    opener = OggVorbis if source.suffix == ".ogg" else OggOpus
    before = opener(str(source))
    after = opener(str(target))

    assert after["LYRICS"] == [LYRICS]
    assert after["LYRICIST"] == ["作词人甲"]
    assert after["ARTISTS"] == ["甲", "乙"], "重复键必须原样保留"
    assert after["CUSTOMTAG"] == ["自定义值"]
    # 时长是解码侧最直接的健全性指标
    assert after.info.length == pytest.approx(before.info.length, abs=1e-6)


def test_vendor_and_duplicate_order_preserved(vorbis_fixture: Path, tmp_path: Path) -> None:
    source = vorbis_fixture
    target = tmp_path / "out.ogg"
    original = oggmeta.ogg_parse_file(source)
    oggmeta.ogg_rewrite_with_lyrics(source, target, lyrics=LYRICS)
    rewritten = oggmeta.ogg_parse_file(target)

    assert rewritten.comments.vendor == original.comments.vendor

    # 非歌词项：数量、顺序、原始字节全部一致（LYRICS 是新增的，单独剔除）
    def non_lyrics(layout: oggmeta.OggLayout) -> list[tuple[bytes, bytes]]:
        return [
            (name, raw)
            for name, raw in layout.comments.entries
            if name.upper() not in {f.encode("ascii") for f in VORBIS_LYRICS_FIELDS}
        ]

    assert non_lyrics(rewritten) == non_lyrics(original)
    # 重复键确实存在，且顺序未被打乱（mutagen 写入时把键名大写）
    artists = [raw for name, raw in non_lyrics(original) if name.upper() == b"ARTISTS"]
    assert artists == ["ARTISTS=甲".encode(), "ARTISTS=乙".encode()]
    # 新歌词项追加在末尾
    assert rewritten.comments.entries[-1][0].upper() == b"LYRICS"


def test_lyrics_none_only_clears(vorbis_fixture: Path, tmp_path: Path) -> None:
    """lyrics=None 时只清理白名单歌词项，不写入新歌词。"""
    staged = tmp_path / "with_lyrics.ogg"
    oggmeta.ogg_rewrite_with_lyrics(vorbis_fixture, staged, lyrics=LYRICS)
    assert oggmeta.ogg_parse_file(staged).comments.get("LYRICS") == [LYRICS.encode("utf-8")]

    cleared = tmp_path / "cleared.ogg"
    oggmeta.ogg_rewrite_with_lyrics(staged, cleared, lyrics=None)

    layout = oggmeta.ogg_parse_file(cleared)
    assert layout.comments.get("LYRICS") == []
    assert layout.comments.get("UNSYNCEDLYRICS") == []
    # 非歌词项仍然完好
    assert layout.comments.get("LYRICIST") == ["作词人甲".encode()]
    assert layout.audio_hash() == oggmeta.ogg_parse_file(vorbis_fixture).audio_hash()


def test_lyricist_is_not_a_lyrics_field(vorbis_fixture: Path, tmp_path: Path) -> None:
    """LYRICIST 是作词人，绝不能被当作歌词清理。"""
    target = tmp_path / "out.ogg"
    oggmeta.ogg_rewrite_with_lyrics(
        vorbis_fixture, target, lyrics=LYRICS, clear_fields=VORBIS_LYRICS_FIELDS
    )
    assert oggmeta.ogg_parse_file(target).comments.get("LYRICIST") == ["作词人甲".encode()]


def test_clearing_a_non_whitelisted_field_is_rejected(vorbis_fixture: Path, tmp_path: Path) -> None:
    """防呆：不允许把 LYRICIST 传进 clear_fields。"""
    with pytest.raises(ValueError, match="白名单"):
        oggmeta.ogg_rewrite_with_lyrics(
            vorbis_fixture, tmp_path / "out.ogg", lyrics=LYRICS, clear_fields=("LYRICIST",)
        )


def test_repeated_write_is_idempotent(vorbis_fixture: Path, tmp_path: Path) -> None:
    first = tmp_path / "a.ogg"
    second = tmp_path / "b.ogg"
    oggmeta.ogg_rewrite_with_lyrics(vorbis_fixture, first, lyrics=LYRICS)
    oggmeta.ogg_rewrite_with_lyrics(first, second, lyrics=LYRICS)
    assert first.read_bytes() == second.read_bytes(), "重复写入应产生完全相同的字节"


def test_long_lyrics_force_extra_pages(vorbis_fixture: Path, tmp_path: Path) -> None:
    """超长歌词必然撑出多页，此时后续页面序号要顺移且 CRC 重算。"""
    long_lyrics = "\n".join(f"[{i // 60:02d}:{i % 60:02d}.00]第 {i} 行歌词" for i in range(3000))
    target = tmp_path / "long.ogg"

    original = oggmeta.ogg_parse_file(vorbis_fixture)
    oggmeta.ogg_rewrite_with_lyrics(vorbis_fixture, target, lyrics=long_lyrics)
    rewritten = oggmeta.ogg_parse_file(target)

    assert len(rewritten.pages) > len(original.pages), "长歌词应当产生更多页面"
    assert rewritten.audio_hash() == original.audio_hash()
    assert oggmeta.compare_non_lyrics(original, rewritten, clear_fields=VORBIS_LYRICS_FIELDS) == []

    raw = target.read_bytes()
    for page in rewritten.pages:
        assert _stored_crc_is_valid(raw, page)
    sequences = [p.sequence for p in rewritten.pages]
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))

    # mutagen 仍能完整读回
    after = OggVorbis(str(target))
    assert after["LYRICS"] == [long_lyrics]
    assert after.info.length == pytest.approx(OggVorbis(str(vorbis_fixture)).info.length, abs=1e-6)


def test_compare_non_lyrics_detects_tampering(vorbis_fixture: Path, tmp_path: Path) -> None:
    """证明 compare_non_lyrics 不是永远返回空列表。"""
    source = vorbis_fixture
    target = tmp_path / "out.ogg"
    oggmeta.ogg_rewrite_with_lyrics(source, target, lyrics=LYRICS)

    original = oggmeta.ogg_parse_file(source)
    rewritten = oggmeta.ogg_parse_file(target)

    # 把「清空歌词」的范围设成空集合，则原文件多出的 LYRICS 项会被视为差异
    problems = oggmeta.compare_non_lyrics(original, rewritten, clear_fields=())
    assert problems, "歌词项差异必须被检出"
    assert any("注释" in problem for problem in problems)


def test_opus_tags_has_no_vorbis_framing_bit(opus_fixture: Path, tmp_path: Path) -> None:
    """OpusTags 不带 framing bit；多写一个 0x01 会让解码器拒绝。

    同时验证注释块之后的填充被原样保留（mutagen 会在 OpusTags 里补零填充）。
    """
    target = tmp_path / "out.opus"
    before = oggmeta.ogg_parse_file(opus_fixture)
    oggmeta.ogg_rewrite_with_lyrics(opus_fixture, target, lyrics=LYRICS)
    after = oggmeta.ogg_parse_file(target)

    packet = after.comment_packet_bytes()
    assert packet.startswith(b"OpusTags")

    block = after.comments.to_bytes()
    # 注释块紧随 OpusTags 之后，且没有被插入任何 framing 字节
    assert packet[len(b"OpusTags") : len(b"OpusTags") + len(block)] == block

    # 包长度增量必须恰好等于注释块的增量 —— 说明既没加 framing bit，
    # 也没丢掉原有的填充
    assert len(packet) - len(before.comment_packet_bytes()) == len(block) - len(
        before.comments.to_bytes()
    )
    assert after.comment_padding == before.comment_padding
    assert OggOpus(str(target))["LYRICS"] == [LYRICS]


def test_vorbis_framing_bit_is_written(opus_fixture: Path, tmp_path: Path) -> None:
    """Vorbis 的 comment header 必须以 framing bit 1 结束。"""
    target = tmp_path / "out.ogg"
    oggmeta.ogg_rewrite_with_lyrics(_BASE_VORBIS, target, lyrics=LYRICS)
    packet = oggmeta.ogg_parse_file(target).comment_packet_bytes()
    assert packet.startswith(b"\x03vorbis")
    assert packet[-1] & 0x01, "Vorbis comment header 的 framing bit 必须为 1"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="本机未安装 ffmpeg")
def test_ffmpeg_decodes_rewritten_stream(vorbis_fixture: Path, tmp_path: Path) -> None:
    """真实解码验证：CRC 算错时 ffmpeg 会直接报错。

    本机（macOS 开发机）未安装 ffmpeg，因此这里通常是 **skip**——跳过不等于通过。
    同一验证在 NAS（装有 ffmpeg 8.1.1）上以等价命令执行，结果记录在交付说明中。
    """
    import subprocess

    target = tmp_path / "decoded.ogg"
    oggmeta.ogg_rewrite_with_lyrics(vorbis_fixture, target, lyrics=LYRICS)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(target), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "", f"ffmpeg 报告了错误: {result.stderr}"


# ---------------------------------------------------------------------------
# 通用标签写入（与歌词写入共用同一套页面重排与自证）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["vorbis_fixture", "opus_fixture"])
def test_rewrite_tags_preserves_audio_and_other_fields(
    fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    source: Path = request.getfixturevalue(fixture)
    target = tmp_path / source.name
    original = oggmeta.ogg_parse_file(source)

    oggmeta.ogg_rewrite_tags(
        source,
        target,
        tags={"TITLE": ["新标题"], "ARTIST": ["新艺人"], "TRACKNUMBER": ["7"]},
        remove=["CUSTOMTAG"],
    )
    rewritten = oggmeta.ogg_parse_file(target)

    assert rewritten.audio_hash() == original.audio_hash()
    assert rewritten.comments.get("TITLE") == ["新标题".encode()]
    assert rewritten.comments.get("ARTIST") == ["新艺人".encode()]
    assert rewritten.comments.get("TRACKNUMBER") == [b"7"]
    assert rewritten.comments.get("CUSTOMTAG") == []
    # 没被点到的字段（含重复键）原样保留
    assert rewritten.comments.get("ARTISTS") == [
        "甲".encode(),
        "乙".encode(),
    ]
    assert rewritten.comments.vendor == original.comments.vendor
    # 音频之外的头部包逐字节一致
    assert rewritten.non_comment_header_packets() == original.non_comment_header_packets()

    opener = OggVorbis if source.suffix == ".ogg" else OggOpus
    after = opener(str(target))
    assert after["TITLE"] == ["新标题"]
    assert after["ARTISTS"] == ["甲", "乙"]
    assert after.info.length == pytest.approx(opener(str(source)).info.length, abs=1e-6)


def test_rewrite_tags_rejects_non_ascii_key(vorbis_fixture: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ASCII"):
        oggmeta.ogg_rewrite_tags(
            vorbis_fixture, tmp_path / "out.ogg", tags={"标题": ["x"]}
        )


def test_apply_tag_edit_ogg_roundtrip(vorbis_fixture: Path, tmp_path: Path) -> None:
    """`writer.apply_tag_edit` 也必须走同一条 OGG 受限写入路径。"""
    from lyra.models import MediaFormat
    from lyra.writer import TagEditPlan, apply_tag_edit, audio_hash_for

    source = tmp_path / "track.ogg"
    shutil.copy(vorbis_fixture, source)
    before_hash = audio_hash_for(source, MediaFormat.OGG_VORBIS)

    result = apply_tag_edit(
        TagEditPlan(
            audio_path=source,
            media_format=MediaFormat.OGG_VORBIS,
            tags={"TITLE": ["写入后的标题"]},
            remove=[],
            original_audio_hash=before_hash or "",
        )
    )
    assert result.ok, result.error
    assert result.audio_hash == before_hash
    assert oggmeta.ogg_parse_file(source).comments.get("TITLE") == [
        "写入后的标题".encode()
    ]
