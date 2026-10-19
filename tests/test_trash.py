"""回收站生命周期的测试。

## 为什么必须有

实现「删除」时只把文件移进 `data/trash/`，**没有做列表、还原、清空**，
却在界面上写「移入回收站」——等于承诺了可恢复却不给恢复入口。

而且最初的命名是 `时间戳__文件名`，**丢掉了原目录**：同名文件可能来自
不同专辑，还原根本无从谈起。现在结构是 `trash/<批次>/<原相对路径>`。

本文件固定三件事：
1. 删除保留原相对路径（否则无法还原）；
2. 还原会把文件搬回原位，且**拒绝覆盖**已存在的文件；
3. 清空必须显式确认，且只删指定批次。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


@pytest.fixture
def client(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    (lib / "专辑A").mkdir(parents=True)
    (lib / "专辑B").mkdir(parents=True)
    flac_factory(lib / "专辑A" / "歌.flac")
    (lib / "专辑A" / "歌.lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    # 同名文件来自不同专辑——这正是「只存文件名」无法还原的原因
    flac_factory(lib / "专辑B" / "歌.flac")

    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        c.cfg = cfg          # type: ignore[attr-defined]
        c.lib = lib          # type: ignore[attr-defined]
        yield c


def _delete(client, rel: str):
    resp = client.post("/api/v1/files/delete", json={"root": "lib", "path": rel})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_删除保留原相对路径(client):
    """必须能知道它原来在哪，否则无法还原。"""
    _delete(client, "专辑A/歌.flac")

    data = client.get("/api/v1/trash").json()
    assert data["total"] >= 1
    paths = [i["rel_path"] for b in data["batches"] for i in b["items"]]
    assert "专辑A/歌.flac" in paths, f"回收站里应保留原目录结构，实际: {paths}"


def test_删除音频会一并移走同名歌词(client):
    """不改走 .lrc 的话它就变成孤儿，播放器再也读不到。"""
    _delete(client, "专辑A/歌.flac")

    paths = [i["rel_path"] for b in client.get("/api/v1/trash").json()["batches"]
             for i in b["items"]]
    assert "专辑A/歌.lrc" in paths


def test_还原放回原位(client):
    _delete(client, "专辑A/歌.flac")
    batch = client.get("/api/v1/trash").json()["batches"][0]["batch"]

    resp = client.post("/api/v1/trash/restore",
                       json={"root": "lib", "batch": batch, "path": "专辑A/歌.flac"})
    assert resp.status_code == 200, resp.text
    assert (client.lib / "专辑A" / "歌.flac").is_file()
    # 删除时音频与同名 .lrc 一起移走，还原也必须一起——
    # 只还音频会让歌词留在回收站里变成孤儿，而用户以为自己「还原了」
    assert (client.lib / "专辑A" / "歌.lrc").is_file()
    assert client.get("/api/v1/trash").json()["total"] == 0


def test_还原拒绝覆盖已存在的文件(client):
    """原位若已有同名文件，那可能是用户有意放的新文件，不能覆盖。"""
    _delete(client, "专辑A/歌.flac")
    batch = client.get("/api/v1/trash").json()["batches"][0]["batch"]
    (client.lib / "专辑A" / "歌.flac").write_bytes(b"fLaC" + b"\x00" * 32)

    resp = client.post("/api/v1/trash/restore",
                       json={"root": "lib", "batch": batch, "path": "专辑A/歌.flac"})
    assert resp.status_code == 409
    assert "拒绝覆盖" in resp.json()["detail"]


def test_清空必须显式确认(client):
    """永久删除不可恢复，缺了确认就不能执行。"""
    _delete(client, "专辑A/歌.flac")

    resp = client.post("/api/v1/trash/empty", json={})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]
    assert client.get("/api/v1/trash").json()["total"] >= 1, "未确认时不该删掉任何东西"


def test_确认后清空(client):
    _delete(client, "专辑A/歌.flac")
    batch = client.get("/api/v1/trash").json()["batches"][0]["batch"]

    resp = client.post("/api/v1/trash/empty", json={"batch": batch, "confirm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_files"] >= 1
    assert client.get("/api/v1/trash").json()["total"] == 0


def test_清空只影响指定批次(client):
    """按批次清空是为了「只丢掉这次的误删，保留上个月的」。"""
    _delete(client, "专辑A/歌.flac")
    first = client.get("/api/v1/trash").json()["batches"][0]["batch"]

    # 同一秒内的删除会归入同一批次，所以这里手工造第二个批次，
    # 才能真正验证「只清指定批次」
    trash = client.cfg.storage.resolved_data_dir / "trash"   # type: ignore[attr-defined]
    other = trash / "29990101-000000" / "专辑B"
    other.mkdir(parents=True)
    (other / "歌.flac").write_bytes(b"fLaC")

    client.post("/api/v1/trash/empty", json={"batch": first, "confirm": True})

    remaining = client.get("/api/v1/trash").json()
    assert all(b["batch"] != first for b in remaining["batches"]), "被清空的批次不该还在"
    assert any(b["batch"] == "29990101-000000" for b in remaining["batches"]), \
        "另一个批次不该被牵连"
