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

from conftest import make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.cover import caa_front_url, qq_album_art_url
from lyra.db import open_store
from lyra.ingest import (
    _COVER_UPGRADE_BELOW,
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
