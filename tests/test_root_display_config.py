"""受管根的显示名与宿主机路径配置。

## 为什么需要这个测试

用户的实际场景：容器里挂载点是 `/library`（内部名 `library`），磁盘上
的真实目录是 `/vol2/1000/Music/Library`。曲库选择下拉显示的是
「library」，与磁盘上的「Library」对不上——使用者会怀疑自己选错了
目录。

2.2.8 修过一版，但默认显示名是从**容器内路径**推导的（`/library` →
「library」），所以问题没解决，还把「用户自己取名」这件事做成了
settings.root_labels 这个独立映射，没法和路径一起编辑。

现在：
- 显示名顺序：**自定义 > 宿主机目录名 > 容器内目录名 > 内部名**；
- 设置页可同时编辑显示名与宿主机路径；
- 曲库下拉展开时显示路径，不再只有一个名字。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store, sync_root_profiles
from lyra.models import RootRole


def _cfg(tmp_path: Path, host_path: str = "/vol2/1000/Music/Library") -> Config:
    lib = tmp_path / "Library"
    lib.mkdir()
    return Config(
        roots=[
            RootConfig(
                name="library", role=RootRole.LIBRARY, path=str(lib),
                host_path=host_path,
            ),
        ],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def _client(tmp_path: Path, host_path: str = "/vol2/1000/Music/Library") -> TestClient:
    cfg = _cfg(tmp_path, host_path)
    return TestClient(create_app(cfg))


def test_默认显示宿主机目录名(tmp_path: Path):
    """容器内是 `/…/Library`（本机测试用临时目录），宿主机是
    `/vol2/1000/Music/Library`——默认显示名取宿主机目录名。"""
    client = _client(tmp_path)
    root = client.get("/api/v1/summary").json()["roots"][0]
    assert root["label"] == "Library", (
        f"默认显示名应是宿主机目录名，实际 {root['label']!r}"
    )
    assert root["host_path"] == "/vol2/1000/Music/Library"


def test_设置显示名与宿主机路径(tmp_path: Path):
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"roots": [{
        "name": "library",
        "display_name": "我的曲库",
        "host_path": "/vol2/1000/Music/Library",
    }]})
    assert r.status_code == 200
    root = client.get("/api/v1/summary").json()["roots"][0]
    assert root["label"] == "我的曲库"
    assert root["display_path"] == "/vol2/1000/Music/Library"


def test_清空显示名回到宿主机目录名(tmp_path: Path):
    client = _client(tmp_path)
    client.put("/api/v1/settings/roots", json={"roots": [{
        "name": "library", "display_name": "我的曲库",
        "host_path": "/vol2/1000/Music/Library",
    }]})
    client.put("/api/v1/settings/roots", json={"roots": [{
        "name": "library", "display_name": "", "host_path": "/vol2/1000/Music/Library",
    }]})
    root = client.get("/api/v1/summary").json()["roots"][0]
    assert root["label"] == "Library", "清空自定义后回落到宿主机目录名"


def test_宿主机路径必须是绝对路径(tmp_path: Path):
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"roots": [{
        "name": "library", "display_name": "x", "host_path": "相对路径",
    }]})
    assert r.status_code == 400


def test_未知曲库名拒绝(tmp_path: Path):
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"roots": [{
        "name": "libray", "display_name": "拼错", "host_path": "/x",
    }]})
    assert r.status_code == 400


def test_同步根档案写入宿主机路径(tmp_path: Path):
    """配置里的 host_path 要进状态库——界面要靠它显示真实位置。"""
    cfg = _cfg(tmp_path)
    store = open_store(cfg.storage.resolved_data_dir)
    sync_root_profiles(store, cfg.roots)
    row = store.conn.execute(
        "SELECT name, path, host_path FROM roots WHERE name='library'"
    ).fetchone()
    assert row is not None
    assert row["host_path"] == "/vol2/1000/Music/Library"
    store.close()


def test_重复同步不覆盖用户设置的显示名(tmp_path: Path):
    """扫描每次都会 upsert_root，不能把用户取的显示名冲掉。"""
    cfg = _cfg(tmp_path)
    store = open_store(cfg.storage.resolved_data_dir)
    sync_root_profiles(store, cfg.roots)
    root_id = store.get_root_id("library")
    assert root_id is not None
    store.set_root_display(root_id, display_name="我的曲库")
    sync_root_profiles(store, cfg.roots)
    row = store.conn.execute(
        "SELECT display_name FROM roots WHERE id=?", (root_id,)
    ).fetchone()
    assert row["display_name"] == "我的曲库"
    store.close()


def test_旧写法仍可用(tmp_path: Path):
    """2.2.8 的 `{"labels": {...}}` 写法继续支持（兼容旧前端与脚本）。"""
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"labels": {"library": "旧写法"}})
    assert r.status_code == 200
    root = client.get("/api/v1/summary").json()["roots"][0]
    assert root["label"] == "旧写法"


# ---------------------------------------------------------------------------
# 新增曲库（运行时根）
# ---------------------------------------------------------------------------


def test_新增曲库并参与配置(tmp_path: Path):
    """新增的曲库要进 cfg.roots——否则扫描与监听看不到它。"""
    client = _client(tmp_path)
    inbox = tmp_path / "Library" / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    r = client.post("/api/v1/settings/roots", json={
        "name": "inbox", "path": str(inbox), "role": "inbox",
        "host_path": "/vol2/1000/Music/Inbox", "display_name": "收件箱",
    })
    assert r.status_code == 200
    roots = client.get("/api/v1/summary").json()["roots"]
    names = {x["name"] for x in roots}
    assert "inbox" in names
    entry = next(x for x in roots if x["name"] == "inbox")
    assert entry["label"] == "收件箱"


def test_新增曲库路径必须存在(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/api/v1/settings/roots", json={
        "name": "nope", "path": str(tmp_path / "不存在"),
    })
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_新增曲库不能重名(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/api/v1/settings/roots", json={
        "name": "library", "path": str(tmp_path / "Library"),
    })
    assert r.status_code == 409


def test_子目录作为独立曲库时父库自动忽略(tmp_path: Path):
    """「主库 + 主库里的收件箱」是常见用法。

    允许添加，但要把子目录加进父库的忽略列表——否则同一批文件会被
    两个根各扫一遍。
    """
    client = _client(tmp_path)
    inbox = tmp_path / "Library" / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    r = client.post("/api/v1/settings/roots", json={
        "name": "inbox", "path": str(inbox),
    })
    assert r.status_code == 200, r.json()
    names = {x["name"] for x in client.get("/api/v1/summary").json()["roots"]}
    assert "inbox" in names


def test_不能把包含现有曲库的目录设为新曲库(tmp_path: Path):
    """父目录里已有曲库 → 新曲库会重复扫描，必须拒绝。"""
    client = _client(tmp_path)
    r = client.post("/api/v1/settings/roots", json={
        "name": "parent", "path": str(tmp_path),
    })
    assert r.status_code == 400
    assert "重复扫描" in r.json()["detail"]


def test_新增曲库拒绝系统目录(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/api/v1/settings/roots", json={"name": "root", "path": "/"})
    assert r.status_code == 400


def test_新增曲库名称格式受限(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/api/v1/settings/roots", json={
        "name": "有中文", "path": str(tmp_path / "Library"),
    })
    assert r.status_code == 400
