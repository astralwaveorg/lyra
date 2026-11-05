"""封面链路的三个优化。

## 线上实测

- 全库 60% 的内嵌封面是 **500×500**（QQ 来源的默认尺寸），大屏/车机上
  发糊——QQ 与 CAA 都支持更大的尺寸，请求时直接要大的；
- 全库 0 个目录有独立封面图（全部靠内嵌 + 联网获取）；
- 覆盖率已 99.8%（2062/2067），剩下的 5 首缺的是**身份**（K-pop OST
  连标题都没有），不是封面来源问题。

本文件固定：大尺寸请求、低分辨率封面升级路径、已有好封面不重写。
"""

from __future__ import annotations

from pathlib import Path

from conftest import build_streaminfo, build_vorbis_comment, make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.cover import caa_front_url, qq_album_art_url
from lyra.db import open_store
from lyra.ingest import (
    _COVER_UPGRADE_BELOW,
    _COVER_UPGRADE_MIN_GAIN,
    _cover_is_worth_upgrading,
    embedded_cover_pixels,
    run_cover_stage,
    self_serve_cover_block,
)


def test_QQ封面URL支持大尺寸():
    url = qq_album_art_url("testmid", size=1500)
    assert "T002R1500x1500M000testmid.jpg" in url


def test_CAA封面URL支持大尺寸():
    url = caa_front_url("release-id", size=1200)
    assert "front-1200" in url


def test_嵌入封面尺寸读取(tmp_path: Path):
    """读不出尺寸返回 None（不是 0）——0 会被当成「肯定要升级」。"""
    p = tmp_path / "歌.flac"
    make_flac(p, entries=(("TITLE", "歌"),))
    # 测试文件没有封面
    assert embedded_cover_pixels(p, __import__("lyra.models", fromlist=["MediaFormat"]).MediaFormat.FLAC) is None


def test_升级线是25万像素():
    """500×500 = 250k，正是 QQ 默认尺寸——低于它才尝试升级。"""
    assert _COVER_UPGRADE_BELOW == 250_000


def test_无封面文件走原有嵌入链路(tmp_path: Path):
    """升级路径不影响「没有封面」的文件：它们照常走 sibling → CAA。"""
    lib = tmp_path / "lib"
    p = lib / "歌手" / "专辑" / "歌.flac"
    p.parent.mkdir(parents=True)
    make_flac(p, entries=(("TITLE", "歌"), ("ARTIST", "歌手"), ("ALBUM", "专辑")))
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    result = run_cover_stage(store, cfg, cfg.roots[0], rel_paths=["歌手/专辑/歌.flac"])
    steps = result["steps"]
    assert steps, "应有步骤记录"
    # 无封面时不会是 skipped「已有内嵌封面」
    assert steps[0][1] in ("done", "review", "failed")
    store.close()


def test_缓存大图能触发升级(tmp_path: Path):
    """专辑缓存里已有更大的图 → 不联网也能升级。"""
    lib = tmp_path / "lib"
    p = lib / "歌手" / "专辑" / "歌.flac"
    p.parent.mkdir(parents=True)
    make_flac(p, entries=(
        ("TITLE", "歌"), ("ARTIST", "歌手"), ("ALBUM", "专辑"),
        ("MUSICBRAINZ_ALBUMID", "rel-1"),
    ))
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    # 造一张大图放进专辑缓存
    from lyra.cover import detect_image

    big_png = tmp_path / "big.png"
    # 1x1 的 PNG 太小；用 build_picture_block 需要真图。造一个 800x800 纯色 PNG。
    import struct
    import zlib

    width = height = 800
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    big_png.write_bytes(png)
    info = detect_image(png)
    assert info.width == 800

    store.upsert_cover_album(
        album_key="rel-1", source="caa", image_hash="x" * 64,
        file_path=str(big_png), mime="image/png",
        width=800, height=800,
    )
    # 让 get_cover_album 返回 ready 状态
    store.conn.execute(
        "UPDATE cover_albums SET state='ready' WHERE album_key='rel-1'"
    )
    store.conn.commit()

    meta = __import__("lyra.formats", fromlist=["read_audio_meta"]).read_audio_meta(p, lib)
    got = self_serve_cover_block(
        store, cfg, cfg.roots[0], p, meta, cfg.policy.cover,
    )
    assert got is not None, "缓存里的大图应能直接用于升级"
    store.close()


# --------------------------------------------------------------------------
# 封面升级死循环（2026-11-05 线上实测）
#
# 内嵌封面 500×495 = 247500 像素，恒低于 250000 的升级线；而能拿到的最好
# 也只有 500×500。于是「偏小 → 换 → 换回来还是偏小 → mtime 变 → inotify
# 再触发」形成无限重写循环，实测在 NAS 上写掉 1.38 TB。
#
# 修法：换封面要有实际收益——新封面至少要比现内嵌的大 1.5 倍。
# --------------------------------------------------------------------------


def _png(width: int, height: int) -> bytes:
    """造一张纯色 PNG。"""
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _make_flac_with_cover(path: Path, png: bytes, width: int, height: int) -> Path:
    """写出一个带内嵌封面（type 3）的最小 FLAC。"""

    def u32(value: int) -> bytes:
        return value.to_bytes(4, "big")

    mime = b"image/png"
    payload = (
        u32(3)  # picture type: front cover
        + u32(len(mime))
        + mime
        + u32(0)  # description
        + u32(width)
        + u32(height)
        + u32(24)  # color depth
        + u32(0)  # indexed colors
        + u32(len(png))
        + png
    )
    blocks = [
        (0, build_streaminfo()),
        (4, build_vorbis_comment(vendor=b"lyra-test", entries=(("TITLE", "歌"),))),
        (6, payload),
    ]
    data = bytearray(b"fLaC")
    for index, (block_type, block_payload) in enumerate(blocks):
        is_last = index == len(blocks) - 1
        data.append((0x80 if is_last else 0x00) | block_type)
        data += len(block_payload).to_bytes(3, "big")
        data += block_payload
    data += b"\xff" * 256
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


def test_换封面要有实际收益():
    """500×495（247500）配 500×500（250000）不算升级——否则永远重写。"""
    assert not _cover_is_worth_upgrading(247_500, 250_000)
    # 明显更大才值得换
    assert _cover_is_worth_upgrading(247_500, 800_000)
    # 新图仍小于升级线，不换
    assert not _cover_is_worth_upgrading(247_500, 200_000)
    # 旧封面尺寸未知时，按升级线判断
    assert _cover_is_worth_upgrading(None, 250_000)
    assert not _cover_is_worth_upgrading(None, 100_000)
    # 新图尺寸未知，一律不换
    assert not _cover_is_worth_upgrading(247_500, None)


def test_收益倍数是1_5倍():
    assert _COVER_UPGRADE_MIN_GAIN == 1.5


def test_没有明显更大的封面时不重写(tmp_path: Path, monkeypatch):
    """线上场景：内嵌 500×495，能换到的只有 500×500 —— 不能反复重写文件。"""
    lib = tmp_path / "lib"
    p = lib / "歌手" / "专辑" / "歌.flac"
    p.parent.mkdir(parents=True)
    _make_flac_with_cover(p, _png(500, 495), 500, 495)

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.cover.auto_write = True
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )

    writes: list[int] = []
    monkeypatch.setattr(
        "lyra.ingest.self_serve_cover_block",
        lambda *a, **k: _png(500, 500),
    )
    monkeypatch.setattr(
        "lyra.ingest._embed_block",
        lambda *a, **k: writes.append(1) or True,
    )

    result = run_cover_stage(
        store, cfg, cfg.roots[0], rel_paths=["歌手/专辑/歌.flac"],
    )
    assert writes == [], "没有明显更大的封面时不应重写文件"
    assert result["steps"][0][1] == "skipped"
    store.close()


def test_明显更大的封面仍然会升级(tmp_path: Path, monkeypatch):
    """修复不能把正常的封面升级也一起堵死。"""
    lib = tmp_path / "lib"
    p = lib / "歌手" / "专辑" / "歌.flac"
    p.parent.mkdir(parents=True)
    _make_flac_with_cover(p, _png(500, 495), 500, 495)

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.cover.auto_write = True
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )

    writes: list[int] = []
    monkeypatch.setattr(
        "lyra.ingest.self_serve_cover_block",
        lambda *a, **k: _png(1000, 1000),
    )
    monkeypatch.setattr(
        "lyra.ingest._embed_block",
        lambda *a, **k: writes.append(1) or True,
    )

    result = run_cover_stage(
        store, cfg, cfg.roots[0], rel_paths=["歌手/专辑/歌.flac"],
    )
    assert writes == [1], "明显更大的封面应当升级"
    assert result["steps"][0][1] == "done"
    store.close()
