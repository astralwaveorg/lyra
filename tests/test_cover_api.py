"""封面写入接口：修复面板直接上传封面的那条路。

## 为什么补这组测试

「无内嵌封面」以前只给一句「在曲目预览里上传封面图片」——让人关掉对话框、
到另一个页面找这首歌、再点开预览。现在问题面板里就能上传/移除，
于是这条写入路径被**第二个入口**使用，必须自己有用例兜住：

1. 上传后真的写进 PICTURE 块（`has_cover` 为真）；
2. **音频数据逐字节不变**（这是整个项目的底线承诺）；
3. 写完立刻刷新观察，问题徽章当场消失；
4. 移除封面同样不改音频。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.flacmeta import flac_audio_hash
from lyra.models import RootRole
from lyra.pipeline import sync_state

# 1×1 的最小合法 PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def env(tmp_path: Path, flac_factory):
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
    with TestClient(create_app(cfg)) as client:
        yield client, lib


def _q(path="专辑/歌.flac"):
    return {"root": "lib", "path": path}


def test_上传封面会写入且音频不变(env):
    client, lib = env
    audio = lib / "专辑" / "歌.flac"
    before = flac_audio_hash(audio)

    r = client.post("/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items()),
                    content=_PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_cover"] is True
    assert d["cover"]["mime"] == "image/png"
    assert d["cover"]["width"] == 1 and d["cover"]["height"] == 1
    assert d["state_refreshed"] is True

    assert flac_audio_hash(audio) == before, "写封面不能改动音频数据"

    # 读回来确实是这张图
    got = client.get("/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items()))
    assert got.status_code == 200
    assert got.content == _PNG


def test_移除封面也不改音频(env):
    client, lib = env
    audio = lib / "专辑" / "歌.flac"
    client.post("/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items()),
                content=_PNG, headers={"Content-Type": "image/png"})
    before = flac_audio_hash(audio)

    r = client.post(
        "/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items())
        + "&remove=true")
    assert r.status_code == 200, r.text
    assert r.json()["has_cover"] is False
    assert flac_audio_hash(audio) == before
    assert client.get("/api/v1/files/cover?" + "&".join(
        f"{k}={v}" for k, v in _q().items())).status_code == 404


def test_上传后缺陷当场消失(env):
    """写完封面要立刻重读观察，否则界面还挂着「无内嵌封面」30 秒。"""
    client, _lib = env
    before = client.get("/api/v1/tracks", params={"issue": "no_embedded_cover"}).json()
    assert before["total"] == 1, before

    client.post("/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items()),
                content=_PNG, headers={"Content-Type": "image/png"})

    after = client.get("/api/v1/tracks", params={"issue": "no_embedded_cover"}).json()
    assert after["total"] == 0, "上传成功后这个缺陷必须立刻从列表里消失"


def test_拒绝非图片内容(env):
    client, _lib = env
    r = client.post("/api/v1/files/cover?" + "&".join(f"{k}={v}" for k, v in _q().items()),
                    content=b"not an image", headers={"Content-Type": "image/png"})
    # 415：内容不是可识别的图片（与「不支持的文件类型」同一种语义）
    assert r.status_code == 415, r.text
    assert "无法识别的图片格式" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 用同目录的封面文件
# ---------------------------------------------------------------------------


def _png(size=(1, 1)) -> bytes:
    return _PNG


def test_同目录有_cover_就能查到(env):
    client, lib = env
    (lib / "专辑" / "cover.jpg").write_bytes(_PNG)

    r = client.get("/api/v1/files/cover/sibling", params=_q())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] is True
    assert d["name"] == "cover.jpg"
    assert (d["mime"], d["width"], d["height"]) == ("image/png", 1, 1)


def test_同目录没有封面时要如实说没有(env):
    """库里不少单曲/EP 根本没有 cover 文件——不能显示一个点了没反应的按钮。"""
    client, _lib = env
    d = client.get("/api/v1/files/cover/sibling", params=_q()).json()
    assert d["found"] is False


def test_大小写与常见命名都能认(env):
    client, lib = env
    (lib / "专辑" / "Folder.PNG").write_bytes(_PNG)
    d = client.get("/api/v1/files/cover/sibling", params=_q()).json()
    assert d["found"] is True and d["name"] == "Folder.PNG"


def test_不是图片的同名文件不算(env):
    client, lib = env
    (lib / "专辑" / "cover.jpg").write_text("这不是图片", encoding="utf-8")
    d = client.get("/api/v1/files/cover/sibling", params=_q()).json()
    assert d["found"] is False, "内容不是图片时不能声称有封面"
    # 要说清「名字在、但读不出来」，否则会被当成「目录里什么都没」
    assert "读不出来" in (d.get("error") or ""), d


def test_一键嵌入同目录封面且音频不变(env):
    client, lib = env
    (lib / "专辑" / "cover.jpg").write_bytes(_PNG)
    audio = lib / "专辑" / "歌.flac"
    before = flac_audio_hash(audio)

    r = client.post("/api/v1/files/cover/from-dir", json=_q())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_cover"] is True
    assert d["source_file"] == "cover.jpg"
    assert d["state_refreshed"] is True
    assert flac_audio_hash(audio) == before, "嵌入封面不能改动音频数据"


def test_没有封面文件时嵌入要报清楚(env):
    client, _lib = env
    r = client.post("/api/v1/files/cover/from-dir", json=_q())
    assert r.status_code == 404
    assert "cover.jpg" in r.json()["detail"]
