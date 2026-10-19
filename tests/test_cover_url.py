"""按链接下载封面并写入内嵌封面。

## 两条必须守住的东西

1. **这是 SSRF 入口。** 服务端会替使用者发出请求，而容器能访问内网
   （私有网段、回环地址、容器内部网络与云主机的元数据地址）。
   所以只允许 http/https，且域名解析出的**每个**地址都必须是公网地址。
2. **下载到的必须是图片。** 防盗链常常返回 200 + HTML 登录页；
   照收就会把一段 HTML 当封面写进文件。

另外复用现有的写入路径：上传与链接下载只能「来源」不同，落盘动作必须一致
（音频数据逐字节不变、写完立刻刷新状态）。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import lyra.api as api_module
from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.cover import CoverDownloadError, download_cover, ensure_public_host
from lyra.db import open_store
from lyra.flacmeta import flac_audio_hash
from lyra.models import RootRole
from lyra.pipeline import sync_state

# 1×1 的最小合法 PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_URL = "https://y.gtimg.cn/music/photo_new/T011R800x800M000003fHZLo4L4d5C.jpg"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# ---------------------------------------------------------------------------
# 下载层
# ---------------------------------------------------------------------------


def test_正常下载返回图片字节与尺寸():
    def handler(request):
        return httpx.Response(200, content=_PNG,
                              headers={"Content-Type": "image/png"})

    data, info = download_cover(
        _URL, max_bytes=1024, client=_client(handler), resolver=lambda h, p: None)
    assert data == _PNG
    assert (info.mime, info.width, info.height) == ("image/png", 1, 1)


def test_拒绝内网地址():
    """容器能访问 10.x / 127.0.0.1 / 169.254.169.254——这条边界不能松。"""
    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"):
        with pytest.raises(CoverDownloadError) as excinfo:
            ensure_public_host(host, 80)
        assert "非公网" in str(excinfo.value), host


def test_拒绝非_http_协议():
    for url in ("file:///etc/passwd", "ftp://example.com/a.jpg", "/etc/passwd", ""):
        with pytest.raises(CoverDownloadError):
            download_cover(url, max_bytes=1024, client=_client(lambda r: httpx.Response(200)))


def test_拒绝把_HTML_当封面():
    """防盗链常返回 200 + 登录页；必须按图片校验挡掉。"""
    def handler(request):
        return httpx.Response(200, text="<html>请先登录</html>",
                              headers={"Content-Type": "text/html"})

    with pytest.raises(CoverDownloadError) as excinfo:
        download_cover(_URL, max_bytes=1024 * 1024, client=_client(handler),
                       resolver=lambda h, p: None)
    assert "不是可识别的图片" in str(excinfo.value)


def test_超过体积上限就中止():
    big = _PNG + b"\x00" * (2048)
    def handler(request):
        return httpx.Response(200, content=big)

    with pytest.raises(CoverDownloadError) as excinfo:
        download_cover(_URL, max_bytes=1024, client=_client(handler),
                       resolver=lambda h, p: None)
    assert "上限" in str(excinfo.value)


def test_对方返回错误码时给可读说明():
    def handler(request):
        return httpx.Response(403, text="forbidden")

    with pytest.raises(CoverDownloadError) as excinfo:
        download_cover(_URL, max_bytes=1024, client=_client(handler),
                       resolver=lambda h, p: None)
    assert "HTTP 403" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, flac_factory, monkeypatch):
    lib = tmp_path / "lib"
    flac_factory(lib / "专辑" / "歌.flac",
                 entries=(("TITLE", "歌"), ("ARTIST", "歌手"), ("ALBUM", "专辑")))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
    store.close()

    # 接口测试不联网：把下载函数换成可控实现
    def fake_download(url, *, max_bytes):
        if "html" in url:
            raise CoverDownloadError("下载到的不是可识别的图片（无法识别）")
        from lyra.cover import detect_image
        return _PNG, detect_image(_PNG)

    monkeypatch.setattr(api_module, "download_cover", fake_download)

    with TestClient(create_app(cfg)) as client:
        yield client, lib


def test_按链接写入封面且音频不变(env):
    client, lib = env
    audio = lib / "专辑" / "歌.flac"
    before = flac_audio_hash(audio)

    r = client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "专辑/歌.flac", "url": _URL})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_cover"] is True
    assert d["cover"]["mime"] == "image/png"
    assert d["source_url"] == _URL
    assert d["state_refreshed"] is True
    assert flac_audio_hash(audio) == before, "写封面不能改动音频数据"


def test_下载失败要回可读的错误(env):
    client, _lib = env
    r = client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "专辑/歌.flac", "url": "https://example.com/html"})
    assert r.status_code == 422
    assert "不是可识别的图片" in r.json()["detail"]


def test_缺少链接或路径时明确报错(env):
    client, _lib = env
    assert client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "专辑/歌.flac"}).status_code == 400
    assert client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "专辑/歌.flac", "url": "  "}).status_code == 400


def test_只接受音频文件(env):
    client, lib = env
    (lib / "专辑" / "歌.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    r = client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "专辑/歌.lrc", "url": _URL})
    assert r.status_code == 415


def test_越界路径被拒(env):
    client, _lib = env
    r = client.post("/api/v1/files/cover/from-url", json={
        "root": "lib", "path": "../secret.flac", "url": _URL})
    assert r.status_code in (400, 404)
