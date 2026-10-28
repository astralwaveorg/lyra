"""Lyra 测试共享夹具。

为什么需要这些夹具：

1. **真实素材是准确的最终裁判。** 目标曲库里真实存在「`*.flac` 实为 OGG 容器」
   「同一个 Vorbis 键出现多次」这类畸形数据，只有真实文件才能证明代码扛得住。
   因此凡涉及字节级行为的测试，一律复制真实文件到 tmp_path 上操作，
   **绝不对真实曲库做任何写入**。

2. **合成素材让边界可控且测试快速。** 真实 FLAC 每首 8–30 MB，无法用来构造
   十几个精确的畸形用例。这里直接按规范手工拼出最小合法 FLAC / Ogg Vorbis，
   字节数在数百级，且不依赖本机是否装了 ffmpeg（实际本机没有 ffmpeg/oggenc）。

3. **可移植。** 真实素材路径可用环境变量 `LYRA_TEST_LIBRARY` 覆盖；
   素材不存在时相关测试 skip 而不是失败，换一台机器照样能跑其余用例。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# 保证未安装本包时也能导入 src/lyra（换机器/换 venv 时更稳）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mutagen.ogg import OggPage  # noqa: E402

# ---------------------------------------------------------------------------
# 真实素材定位
# ---------------------------------------------------------------------------

REAL_LIBRARY = Path(os.environ.get("LYRA_TEST_LIBRARY", "/nonexistent/lyra-test-library"))

#: 实际含重复 Vorbis 键的真实 FLAC：ARTISTS / ALBUMARTISTS / MUSICBRAINZ_ARTISTID
#: 在字节层面各有 3 条同名条目。mutagen 按键归组后只看到「1 键 3 值」，
#: 因此只有直接解析块结构才能验证「逐字节、按序保留」。
DUP_KEY_FLAC = (
    REAL_LIBRARY
    / "Faouzia, G.E.M. 鄧紫棋 & ILLENIUM"
    / "Still Breathing"
    / "01 Faouzia, G.E.M. 鄧紫棋 & ILLENIUM - Still Breathing.flac"
)

#: 重复键断言的目标键
DUP_KEYS = ("ARTISTS", "ALBUMARTISTS", "MUSICBRAINZ_ARTISTID")


def _first_existing(*candidates: Path) -> Path | None:
    for c in candidates:
        if c.is_file():
            return c
    return None


def find_real_flac() -> Path | None:
    """任意一个真实 FLAC（优先用含重复键的那个）。"""
    if DUP_KEY_FLAC.is_file():
        return DUP_KEY_FLAC
    if not REAL_LIBRARY.is_dir():
        return None
    return next(iter(sorted(REAL_LIBRARY.rglob("*.flac"))), None)


def find_real_mp3() -> Path | None:
    """最小的真实 MP3（本地素材里 MP3 约 4 MB，比 FLAC 小得多）。"""
    if not REAL_LIBRARY.is_dir():
        return None
    found = sorted(REAL_LIBRARY.rglob("*.mp3"), key=lambda p: p.stat().st_size)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# 合成音频构造（不依赖 ffmpeg）
# ---------------------------------------------------------------------------


def build_streaminfo(
    *, sample_rate: int = 44100, channels: int = 2, bits: int = 16, total_samples: int = 132300
) -> bytes:
    """构造 34 字节的 FLAC STREAMINFO 载荷。"""
    body = bytearray()
    body += (4096).to_bytes(2, "big")  # min blocksize
    body += (4096).to_bytes(2, "big")  # max blocksize
    body += (0).to_bytes(3, "big")  # min framesize
    body += (0).to_bytes(3, "big")  # max framesize
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total_samples
    body += packed.to_bytes(8, "big")
    body += b"\x00" * 16  # MD5（本实现有意不依赖它做完整性判断）
    assert len(body) == 34
    return bytes(body)


def build_vorbis_comment(*, vendor: bytes = b"lyra-test", entries=()) -> bytes:
    """构造 Vorbis comment 载荷（不含 Ogg 的 framing bit）。"""
    out = bytearray()
    out += struct.pack("<I", len(vendor)) + vendor
    out += struct.pack("<I", len(entries))
    for key, value in entries:
        raw = f"{key}={value}".encode()
        out += struct.pack("<I", len(raw)) + raw
    return bytes(out)


def make_flac(
    path: Path,
    *,
    entries=(),
    vendor: bytes = b"lyra-test",
    include_padding: bool = True,
    audio: bytes = b"\xff" * 256,
) -> Path:
    """写出一个最小但结构合法的 FLAC。

    必须保留：magic + STREAMINFO + VORBIS_COMMENT(+PADDING) + 至少 1 字节音频帧，
    否则 `flacmeta._parse` 会以「没有音频帧数据」拒绝。
    """
    blocks: list[tuple[int, bytes]] = [
        (0, build_streaminfo()),
        (4, build_vorbis_comment(vendor=vendor, entries=tuple(entries))),
    ]
    if include_padding:
        blocks.append((1, b"\x00" * 64))

    data = bytearray(b"fLaC")
    for index, (block_type, payload) in enumerate(blocks):
        is_last = index == len(blocks) - 1
        data.append((0x80 if is_last else 0x00) | block_type)
        data += len(payload).to_bytes(3, "big")
        data += payload
    data += audio

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


def make_ogg_vorbis(
    path: Path,
    *,
    entries=(),
    sample_rate: int = 44100,
    seconds: float = 3.0,
    serial: int = 0x77,
    vendor: bytes = b"lyra-test",
) -> Path:
    """写出一个最小但可被 mutagen 识别的 Ogg Vorbis。

    用 mutagen 自己的 OggPage.write() 生成页面，因此 Ogg CRC 是正确的。
    真实曲库里「扩展名是 .flac、内容却是 Ogg」的文件正是靠这种构造复现的。
    """
    id_packet = (
        b"\x01vorbis"
        + struct.pack("<I", 0)
        + bytes([2])
        + struct.pack("<I", sample_rate)
        + struct.pack("<i", 0)
        + struct.pack("<i", 128000)
        + struct.pack("<i", 0)
        + bytes([0xB8])
        + b"\x01"
    )
    comment_packet = b"\x03vorbis" + build_vorbis_comment(
        vendor=vendor, entries=tuple(entries)
    ) + b"\x01"
    setup_packet = b"\x05vorbis" + b"\x00" * 16

    first = OggPage()
    first.first = True
    first.serial = serial
    first.sequence = 0
    first.position = 0
    first.packets = [id_packet]

    second = OggPage()
    second.serial = serial
    second.sequence = 1
    second.position = 0
    second.packets = [comment_packet, setup_packet]

    last = OggPage()
    last.last = True
    last.serial = serial
    last.sequence = 2
    last.position = int(sample_rate * seconds)
    last.packets = [b"\x00" * 32]

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        for page in (first, second, last):
            handle.write(page.write())
    return path


def make_mp3(
    path: Path,
    *,
    version: int = 3,
    entries=(("TIT2", "测试曲目"), ("TPE1", "测试艺人")),
    audio: bytes | None = None,
) -> Path:
    """写出一个最小但结构合法的 MP3（ID3v2 + 一帧 MPEG 数据）。

    只要求容器结构正确（供 sniff_container / id3meta 解析），音频帧内容
    无需可播放——字节级写入测试关心的是「音频段逐字节不变」。
    """
    def _synchsafe(value: int) -> bytes:
        return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))

    frames = b""
    for frame_id, value in entries:
        encoding = 0x03 if version == 4 else 0x01
        text = str(value).encode("utf-8" if version == 4 else "utf-16")
        body = bytes((encoding,)) + text
        size = _synchsafe(len(body)) if version == 4 else len(body).to_bytes(4, "big")
        frames += frame_id.encode("ascii") + size + b"\x00\x00" + body

    body = frames + b"\x00" * 128
    tag = b"ID3" + bytes((version, 0, 0)) + _synchsafe(len(body))
    mpeg = audio if audio is not None else b"\xff\xfb\x90\x00" + b"\x00" * 413

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tag + body + mpeg)
    return path


def make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    """最小 PNG：签名 + IHDR（cover.detect_image 从 IHDR 读宽高，不校验 CRC）。"""
    import struct

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + bytes((8, 6, 0, 0, 0))
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return signature + ihdr + b"\x00" * 16


def make_jpeg_bytes(width: int = 4, height: int = 4) -> bytes:
    """最小 JPEG：SOI + SOF0（detect_image 从 SOF 读宽高）+ EOI。"""
    import struct

    sof_body = (
        bytes((8,))                      # 精度
        + struct.pack(">HH", height, width)
        + bytes((3,))                    # 分量数
        + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    return b"\xff\xd8" + sof + b"\xff\xd9"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    """目录内容快照（相对路径 → SHA-256），用于证明「审计没有改动任何文件」。"""
    out: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = sha256_file(path)
    return out


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    """一个空的受管根目录。"""
    root = tmp_path / "lib"
    root.mkdir()
    return root


@pytest.fixture
def flac_factory():
    """返回 make_flac，便于在 tmp 目录里批量造 FLAC。"""
    return make_flac


@pytest.fixture
def ogg_factory():
    """返回 make_ogg_vorbis。"""
    return make_ogg_vorbis


@pytest.fixture
def real_flac(tmp_path: Path) -> Path:
    """真实 FLAC 的临时副本（只读使用原文件，绝不写入原目录）。"""
    src = find_real_flac()
    if src is None:
        pytest.skip(f"真实 FLAC 素材不存在: {REAL_LIBRARY}")
    dst = tmp_path / "real" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


@pytest.fixture
def real_mp3(tmp_path: Path) -> Path:
    """真实 MP3 的临时副本。"""
    src = find_real_mp3()
    if src is None:
        pytest.skip(f"真实 MP3 素材不存在: {REAL_LIBRARY}")
    dst = tmp_path / "real" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


@pytest.fixture
def example_config_path() -> Path:
    """随包分发的示例配置模板。"""
    import lyra

    path = Path(lyra.__file__).resolve().parent / "templates" / "lyra.example.toml"
    if not path.is_file():
        pytest.skip(f"示例配置不存在: {path}")
    return path


@pytest.fixture
def store(tmp_path: Path):
    """临时 SQLite 状态库（已迁移）。"""
    from lyra.db import open_store

    handle = open_store(tmp_path / "data")
    try:
        yield handle
    finally:
        handle.close()


class FakeScandir:
    """os.scandir 的替身，用于构造「子目录位于另一个设备」这种无法真实挂载的场景。"""

    def __init__(self, entries):
        self._entries = list(entries)

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc) -> bool:
        return False


class FakeEntry:
    """os.DirEntry 的替身，只实现 scan._walk 真正访问的属性。"""

    def __init__(self, name: str, path: Path, *, is_dir: bool = False,
                 is_symlink: bool = False, st_dev: int | None = None):
        self.name = name
        self.path = str(path)
        self._is_dir = is_dir
        self._is_symlink = is_symlink
        self._st_dev = st_dev

    def is_symlink(self) -> bool:
        return self._is_symlink

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return not self._is_dir and not self._is_symlink

    def stat(self, follow_symlinks: bool = True):
        return SimpleNamespace(st_dev=self._st_dev)


@pytest.fixture
def fake_scandir_classes():
    return FakeScandir, FakeEntry


@pytest.fixture
def digest_tree():
    """目录内容快照函数，用于证明审计/写入没有改动目标目录。"""
    return tree_digest


@pytest.fixture
def dup_keys():
    """真实素材里出现多次的 Vorbis 键名（字节级重复项）。"""
    return DUP_KEYS


@pytest.fixture
def sha256_of():
    """单文件 SHA-256，用于证明音频字节未变。"""
    return sha256_file
