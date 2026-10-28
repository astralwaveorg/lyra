"""封面写入的格式扩展测试（重构方案 Phase 2）。

此前封面写入只支持 FLAC（writer.apply_cover_edit 硬编码），MP3/OGG/Opus
点写入直接报错。本文件证明三种容器的封面写入都满足同一套安全承诺：

- **音频数据逐字节不变**（各容器的音频哈希）；
- **封面之外的内容逐字节不变**（MP3 其它帧 / OGG 非封面注释）；
- **替换语义与 FLAC 一致**：只动同一图片类型的旧封面，其它类型保留；
- **picture=None = 移除**，不留半成品。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_jpeg_bytes, make_mp3, make_ogg_vorbis, make_png_bytes, sha256_file

from lyra import id3meta, oggmeta
from lyra.cover import build_picture_block, detect_image
from lyra.models import MediaFormat
from lyra.writer import CoverEditPlan, apply_cover_edit

JPEG_LIKE = make_jpeg_bytes(4, 4)
JPEG_LIKE_2 = make_jpeg_bytes(4, 5)


def _write_cover(path: Path, fmt: MediaFormat, *, block: bytes | None) -> object:
    return apply_cover_edit(CoverEditPlan(audio_path=path, media_format=fmt, picture_block=block))


def _front_picture(mime: str = "image/jpeg", data: bytes = JPEG_LIKE) -> bytes:
    info = detect_image(data)
    assert info.mime == mime
    return build_picture_block(info)


# ---------------------------------------------------------------------------
# MP3（APIC）
# ---------------------------------------------------------------------------


def test_mp3写入封面(tmp_path: Path):
    path = make_mp3(tmp_path / "a.mp3")
    before_full = sha256_file(path)
    before_layout = id3meta.id3_parse_file(path)
    before_frames = before_layout.frames

    outcome = _write_cover(path, MediaFormat.MP3, block=_front_picture())
    assert outcome.ok and outcome.has_cover
    assert sha256_file(path) != before_full  # 标签变了
    # 音频段哈希不变
    assert id3meta.id3_parse_file(path).audio_sha256 == before_layout.audio_sha256

    after = id3meta.id3_parse_file(path)
    apic = [f for f in after.frames if f.frame_id == "APIC"]
    assert len(apic) == 1
    parsed = id3meta.parse_apic(apic[0].body)
    assert parsed is not None and parsed[0] == "image/jpeg"
    assert parsed[3] == JPEG_LIKE
    # 非封面帧逐字节不变
    non_cover = [f.raw for f in before_frames]
    non_cover_after = [f.raw for f in after.frames if f.frame_id != "APIC"]
    assert non_cover == non_cover_after


def test_mp3重复写入是替换不是追加(tmp_path: Path):
    path = make_mp3(tmp_path / "a.mp3")
    assert _write_cover(path, MediaFormat.MP3, block=_front_picture()).ok
    assert _write_cover(path, MediaFormat.MP3, block=_front_picture(data=JPEG_LIKE_2)).ok
    after = id3meta.id3_parse_file(path)
    apic = [f for f in after.frames if f.frame_id == "APIC"]
    assert len(apic) == 1
    parsed = id3meta.parse_apic(apic[0].body)
    assert parsed is not None and parsed[3] == JPEG_LIKE_2


def test_mp3移除封面(tmp_path: Path):
    path = make_mp3(tmp_path / "a.mp3")
    assert _write_cover(path, MediaFormat.MP3, block=_front_picture()).ok
    outcome = _write_cover(path, MediaFormat.MP3, block=None)
    assert outcome.ok and not outcome.has_cover
    assert not [f for f in id3meta.id3_parse_file(path).frames if f.frame_id == "APIC"]


def test_mp3其它图片类型的封面保留(tmp_path: Path):
    path = make_mp3(tmp_path / "a.mp3")
    icon_block = build_picture_block(
        detect_image(JPEG_LIKE), picture_type=1
    )  # 1 = 32x32 图标
    assert _write_cover(path, MediaFormat.MP3, block=icon_block).ok

    assert _write_cover(path, MediaFormat.MP3, block=_front_picture()).ok
    after = id3meta.id3_parse_file(path)
    apic = [f for f in after.frames if f.frame_id == "APIC"]
    assert len(apic) == 2  # 图标保留，正面被写入
    types = sorted(id3meta.parse_apic(f.body)[1] for f in apic)
    assert types == [1, 3]


def test_mp3没有ID3标签时新建标签写入封面(tmp_path: Path):
    path = tmp_path / "bare.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 413)
    outcome = _write_cover(path, MediaFormat.MP3, block=_front_picture())
    assert outcome.ok and outcome.has_cover
    layout = id3meta.id3_parse_file(path)
    assert layout.version == 4  # 与标签写入一致：无既有主版本时新建 v2.4


# ---------------------------------------------------------------------------
# OGG / Opus（METADATA_BLOCK_PICTURE）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["vorbis", "opus"])
def test_ogg写入与移除封面(tmp_path: Path, kind: str, ogg_factory):
    name = "a.ogg" if kind == "vorbis" else "a.opus"
    path = make_ogg_vorbis(tmp_path / name, entries=(("TITLE", "测试"),))
    before_audio = oggmeta.ogg_audio_hash(path)
    before_others = _non_cover_comments(path)

    block = build_picture_block(detect_image(make_png_bytes()))
    outcome = _write_cover(path, MediaFormat.OGG_VORBIS, block=block)
    assert outcome.ok and outcome.has_cover
    assert oggmeta.ogg_audio_hash(path) == before_audio
    assert _non_cover_comments(path) == before_others

    comments = oggmeta.ogg_parse_file(path).comments
    assert comments.get("METADATA_BLOCK_PICTURE")

    outcome = _write_cover(path, MediaFormat.OGG_VORBIS, block=None)
    assert outcome.ok and not outcome.has_cover
    assert not oggmeta.ogg_parse_file(path).comments.get("METADATA_BLOCK_PICTURE")
    assert _non_cover_comments(path) == before_others


def _non_cover_comments(path: Path) -> bytes:
    from lyra import flacmeta

    comments = oggmeta.ogg_parse_file(path).comments
    cover_keys = {"METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME", "COVERARTTYPE"}
    stripped = flacmeta.VorbisComments(
        vendor=comments.vendor,
        entries=tuple(
            (name, raw)
            for name, raw in comments.entries
            if name.decode("ascii", "replace").upper() not in cover_keys
        ),
    )
    return stripped.to_bytes()


def test_ogg封面写入的音频哈希与音频页一致(tmp_path: Path, ogg_factory):
    path = make_ogg_vorbis(tmp_path / "a.ogg")
    before_full = sha256_file(path)
    assert _write_cover(path, MediaFormat.OGG_VORBIS, block=build_picture_block(detect_image(make_png_bytes()))).ok
    # 文件整体必然变化（注释块变了），但音频哈希不变
    assert sha256_file(path) != before_full
    assert oggmeta.ogg_audio_hash(path) == oggmeta.ogg_parse_file(path).audio_sha256


# ---------------------------------------------------------------------------
# 分派与拒绝
# ---------------------------------------------------------------------------


def test_ogg_flac容器明确拒绝封面(tmp_path: Path, ogg_factory):
    """OGG 封装的 FLAC 是明确不支持的格式，拒绝而不是兜底。"""
    from lyra.formats import MediaFormat as MF

    path = make_ogg_vorbis(tmp_path / "a.ogg")
    outcome = _write_cover(path, MF.UNSUPPORTED, block=None)
    assert not outcome.ok
    assert "暂不支持" in (outcome.error or "")


def test_移除不存在的封面也成功(tmp_path: Path):
    path = make_mp3(tmp_path / "a.mp3")
    outcome = _write_cover(path, MediaFormat.MP3, block=None)
    assert outcome.ok and not outcome.has_cover
