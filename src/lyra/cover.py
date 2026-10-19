"""封面图片处理：识别格式、读取尺寸、构造各容器的封面结构。

## 为什么需要它

三种容器的封面存放方式完全不同，但都需要「图片字节 + 宽高 + MIME」：

- **FLAC**：独立的 PICTURE 元数据块（二进制结构）
- **OGG / Opus**：`METADATA_BLOCK_PICTURE` 字段，值是**同一个 PICTURE 结构的 base64**
- **MP3**：ID3v2 的 APIC 帧（结构与 PICTURE 类似但不同）

因此把「构造 PICTURE 结构」集中在这里，FLAC 直接用二进制、
OGG 包一层 base64，避免两份实现漂移。

## 为什么自己解析宽高而不引依赖

PICTURE 块要求填宽高、色深、调色板数。这些字段很多工具直接填 0，
但那是「不知道」的意思；自己解析 PNG/JPEG 头部只要几十行，
且**无第三方依赖**——这个项目的依赖已经够多了。
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from collections.abc import Callable
from urllib.parse import urlsplit

__all__ = [
    "CoverInfo",
    "CoverError",
    "CoverDownloadError",
    "detect_image",
    "build_picture_block",
    "download_cover",
    "PICTURE_TYPE_FRONT_COVER",
]

#: ID3/FLAC 通用的图片类型编号。3 = 正面封面，是绝大多数播放器认的那一种。
PICTURE_TYPE_FRONT_COVER = 3

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_WEBP_MAGIC = b"RIFF"


class CoverError(ValueError):
    """图片无法识别或结构异常。"""


#: JPEG 里承载尺寸的 SOF 标记（不含 DHT/JPG/DAC 那几个易混淆的）
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_size(data: bytes) -> tuple[int, int, int]:
    """返回 (宽, 高, 位深)。按段扫描，遇到第一个 SOF 段即可。"""
    index = 2  # 跳过 SOI
    total = len(data)
    while index + 3 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > total:
            break
        length = struct.unpack_from(">H", data, index + 2)[0]
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > total:
                break
            depth = data[index + 4]
            height, width = struct.unpack_from(">HH", data, index + 5)
            return width, height, depth
        index += 2 + length
    raise CoverError("无法从 JPEG 头部解析出尺寸")


def _png_size(data: bytes) -> tuple[int, int, int]:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise CoverError("PNG 结构异常（缺少 IHDR）")
    width, height = struct.unpack_from(">II", data, 16)
    bit_depth = data[24]
    color_type = data[25]
    # 通道数决定「位深」字段该填什么：RGB=3、RGBA=4、灰度=1、灰度+A=2、调色板=1
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
    return width, height, bit_depth * channels


class CoverInfo:
    """一张已识别的封面图片。"""

    __slots__ = ("data", "mime", "width", "height", "depth", "colors")

    def __init__(
        self,
        data: bytes,
        mime: str,
        width: int,
        height: int,
        depth: int = 0,
        colors: int = 0,
    ) -> None:
        self.data = data
        self.mime = mime
        self.width = width
        self.height = height
        self.depth = depth
        self.colors = colors

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"CoverInfo(mime={self.mime!r}, {self.width}x{self.height}, "
            f"{len(self.data)} 字节)"
        )


def detect_image(data: bytes) -> CoverInfo:
    """识别图片格式并读出尺寸。无法识别时抛 `CoverError`。

    只接受音频标签里真正通用的三种（JPEG/PNG/GIF）加 WebP：
    PICTURE 块的 MIME 字段必须准确，认错格式会让播放器读不出封面。
    """
    if not data:
        raise CoverError("图片内容为空")

    if data.startswith(_PNG_MAGIC):
        width, height, depth = _png_size(data)
        return CoverInfo(data, "image/png", width, height, depth)
    if data.startswith(_JPEG_MAGIC):
        width, height, depth = _jpeg_size(data)
        return CoverInfo(data, "image/jpeg", width, height, depth)
    if data.startswith(_GIF_MAGIC[0]) or data.startswith(_GIF_MAGIC[1]):
        if len(data) < 10:
            raise CoverError("GIF 结构异常")
        width, height = struct.unpack_from("<HH", data, 6)
        return CoverInfo(data, "image/gif", width, height)
    if data.startswith(_WEBP_MAGIC) and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        return CoverInfo(data, "image/webp", width, height)

    raise CoverError("无法识别的图片格式（支持 JPEG / PNG / GIF / WebP）")


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise CoverError("无法从 WebP 头部解析出尺寸")


def parse_picture_block(payload: bytes) -> tuple[str, bytes]:
    """从 FLAC/OGG 的 PICTURE 负载里取出 (MIME, 图片字节)。

    只解析到图片数据为止——宽高等字段渲染时用不到。
    """
    if len(payload) < 8:
        raise CoverError("PICTURE 块截断")
    offset = 4  # 跳过 picture type
    mime_len = struct.unpack_from(">I", payload, offset)[0]
    offset += 4
    mime = payload[offset : offset + mime_len].decode("ascii", "replace")
    offset += mime_len
    if offset + 4 > len(payload):
        raise CoverError("PICTURE 块截断（描述长度）")
    desc_len = struct.unpack_from(">I", payload, offset)[0]
    offset += 4 + desc_len
    # 宽、高、色深、调色板数各 4 字节
    offset += 16
    if offset + 4 > len(payload):
        raise CoverError("PICTURE 块截断（图片长度）")
    data_len = struct.unpack_from(">I", payload, offset)[0]
    offset += 4
    data = payload[offset : offset + data_len]
    if not data:
        raise CoverError("PICTURE 块里的图片数据为空")
    return mime, data


def build_picture_block(
    cover: CoverInfo,
    *,
    picture_type: int = PICTURE_TYPE_FRONT_COVER,
    description: str = "",
) -> bytes:
    """构造 FLAC PICTURE 块的负载。

    结构与 OGG 的 `METADATA_BLOCK_PICTURE` **完全相同**——
    后者就是这个字节串的 base64，所以此处可以直接复用。
    """
    mime = cover.mime.encode("ascii")
    desc = description.encode("utf-8")
    parts = [
        struct.pack(">I", picture_type),
        struct.pack(">I", len(mime)),
        mime,
        struct.pack(">I", len(desc)),
        desc,
        struct.pack(">I", cover.width),
        struct.pack(">I", cover.height),
        struct.pack(">I", cover.depth),
        struct.pack(">I", cover.colors),
        struct.pack(">I", len(cover.data)),
        cover.data,
    ]
    return b"".join(parts)


# ---------------------------------------------------------------------------
# 从链接下载封面
# ---------------------------------------------------------------------------
#
# 为什么需要服务端下载：使用者手上常常已经有一个确定的封面地址
# （QQ 音乐的 `y.gtimg.cn/music/photo_new/...`、网易云的 `p1.music.126.net/...`），
# 让它先存成文件再上传是多余的一步；而浏览器直接抓这些图会被跨域挡住。
#
# 这同时是一条 SSRF 入口：容器能访问内网、回环地址、Docker 内部网络与
# 云主机的元数据地址。如果照单全收，任何登录用户都能让服务端去请求它们。
# 所以：只允许 http/https，域名解析出的**每个**地址都必须是公网地址。

class CoverDownloadError(Exception):
    """下载封面失败。消息直接给使用者看，要说清是哪一步出的问题。"""


def _is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global


def ensure_public_host(host: str, port: int) -> None:
    """解析域名并确认每个地址都是公网地址。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise CoverDownloadError(f"无法解析图片地址的域名：{host}") from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise CoverDownloadError(f"无法解析图片地址的域名：{host}")
    for addr in addresses:
        if not _is_public_ip(addr):
            raise CoverDownloadError(
                f"图片地址指向非公网地址（{addr}），已拒绝。只允许公网 http/https 图片。"
            )


def download_cover(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 20.0,
    client=None,
    resolver: Callable[[str, int], None] | None = None,
) -> tuple[bytes, CoverInfo]:
    """下载一张封面并校验它是图片。

    返回 (原始字节, 图片信息)。失败抛 `CoverDownloadError`（中文、可直接展示）。
    `client` 与 `resolver` 是为测试注入的，正常路径留空即可。
    """
    import httpx

    raw = (url or "").strip()
    if not raw:
        raise CoverDownloadError("请填写图片链接")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise CoverDownloadError("只支持 http / https 链接")
    host = parsed.hostname or ""
    if not host:
        raise CoverDownloadError("链接里没有主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    (resolver or ensure_public_host)(host, port)

    own_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            # 这些 CDN 对裸请求会返回 403/HTML：带上常见的 UA 与 Referer
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                # **优先要 JPEG/PNG，不要首推 WebP。** 这些 CDN 会按 Accept
                # 做内容协商：声明 image/webp 就会真的返回 WebP（实际 QQ 音乐
                # 的 .jpg 地址返回了 800×800 的 WebP）。WebP 封面在车机、
                # 老播放器上支持很差，而封面的第一要求是「到处都能显示」。
                "Accept": "image/jpeg,image/png,image/gif,image/*;q=0.8",
            },
        )
    try:
        with client.stream("GET", raw) as response:
            response.raise_for_status()
            # 上限在**读取过程中**判断：Content-Length 可能是假的或缺失
            buffer = bytearray()
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                buffer += chunk
                if len(buffer) > max_bytes:
                    raise CoverDownloadError(
                        f"图片超过 {max_bytes // (1024 * 1024)} MB 上限"
                    )
    except CoverDownloadError:
        raise
    except httpx.HTTPStatusError as exc:
        raise CoverDownloadError(
            f"下载失败：对方返回 HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise CoverDownloadError(f"下载失败：{exc}") from exc
    finally:
        if own_client:
            client.close()

    if not buffer:
        raise CoverDownloadError("下载到的内容为空")
    data = bytes(buffer)
    try:
        info = detect_image(data)
    except CoverError as exc:
        # 防盗链常返回 200 + HTML 页面，必须按图片校验挡掉
        raise CoverDownloadError(
            f"下载到的不是可识别的图片（{exc}）；常见原因是链接需要登录或防盗链"
        ) from exc
    return data, info
