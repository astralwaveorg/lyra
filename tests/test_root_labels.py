"""受管根的显示名映射。

## 为什么需要这个测试

根的内部名（lyra.toml 的 `name`）是 API 标识符，实际目录名可能与之
大小写不同（`library` vs 磁盘上的 `Library`）。整理页选择器按内部名
显示，使用者会怀疑「选错了目录」。

规则：
- 默认显示**实际目录名**（路径的最后一段），自动与目录对上；
- 设置里可自定义（`PUT /api/v1/settings/roots`），例如
  「默认曲库--Library」；空值 = 清除自定义，回到默认；
- 未知名一律 400，防止把拼写错误的键存进库。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


def _client(tmp_path: Path) -> TestClient:
    lib = tmp_path / "Library"  # 注意大写 L：内部名故意与目录大小写不同
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="library", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    from lyra.db import open_store

    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(name="library", role="library", path=str(lib),
                      enabled=True, read_only=False)
    store.close()
    return TestClient(create_app(cfg))


def test_默认显示实际目录名(tmp_path: Path):
    client = _client(tmp_path)
    roots = client.get("/api/v1/summary").json()["roots"]
    assert roots[0]["name"] == "library"
    assert roots[0]["label"] == "Library", "内部名 library，实际目录 Library，应显示后者"


def test_设置显示名后生效且空值清除(tmp_path: Path):
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"labels": {"library": "默认曲库--Library"}})
    assert r.status_code == 200 and r.json()["labels"] == {"library": "默认曲库--Library"}
    label = client.get("/api/v1/summary").json()["roots"][0]["label"]
    assert label == "默认曲库--Library"

    # 空值 = 清除，回到目录名
    r = client.put("/api/v1/settings/roots", json={"labels": {"library": ""}})
    assert r.status_code == 200
    assert client.get("/api/v1/summary").json()["roots"][0]["label"] == "Library"

    # GET 与 PUT 读到同一份
    assert client.get("/api/v1/settings/roots").json() == {"labels": {}}


def test_未知根名拒绝(tmp_path: Path):
    client = _client(tmp_path)
    r = client.put("/api/v1/settings/roots", json={"labels": {"libray": "拼错了"}})
    assert r.status_code == 400
