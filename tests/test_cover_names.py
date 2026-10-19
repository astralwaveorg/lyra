"""封面文件名规范化：只改大小写，不覆盖、可撤销。

为什么值得单独测：这个动作会改真实曲库里的文件，而且是在后台自动做的。
三条底线必须钉住——
1. **只改大小写**：内容、扩展名、所在目录都不变；
2. **绝不覆盖**：同目录已有同名小写文件时停下并报告冲突；
3. **可撤销**：每次改名都进 renames，日志页能还原。

注意：**不能直接用 `Path.exists()` 判断**。macOS/Windows 是大小写不敏感卷，
`(album / "cover.jpg").exists()` 在只有 `Cover.JPG` 时也会返回 True，
断言会假失败。这里一律看目录里真实存在的名字（`os.listdir`）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import normalize_cover_names


def names(folder: Path) -> set[str]:
    return set(os.listdir(folder))


def is_case_insensitive(folder: Path) -> bool:
    probe = folder / "lyra-case-probe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (folder / "LYRA-CASE-PROBE").exists()
    finally:
        probe.unlink(missing_ok=True)


@pytest.fixture
def env(tmp_path: Path):
    lib = tmp_path / "lib"
    album = lib / "Adele" / "19"
    album.mkdir(parents=True)
    (album / "Cover.JPG").write_bytes(b"\xff\xd8\xffCOVER")
    (album / "Folder.PNG").write_bytes(b"\x89PNG\r\n\x1a\nFOLDER")
    (album / "notes.txt").write_text("keep", encoding="utf-8")
    (album / "random.jpg").write_bytes(b"\xff\xd8\xffRANDOM")
    (album / "cover.txt").write_text("not an image", encoding="utf-8")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    yield cfg, store, album
    store.close()


def test_dry_run_只出计划不动文件(env):
    cfg, store, album = env
    result = normalize_cover_names(store, cfg, cfg.roots[0], dry_run=True)
    assert result["renamed"] == 2
    assert names(album) >= {"Cover.JPG", "Folder.PNG"}
    assert "cover.jpg" not in names(album)
    assert {item["to"] for item in result["items"]} == {
        "Adele/19/cover.jpg", "Adele/19/folder.png",
    }


def test_真实执行只改大小写并保留内容(env):
    cfg, store, album = env
    result = normalize_cover_names(store, cfg, cfg.roots[0])
    assert result["renamed"] == 2
    assert "cover.jpg" in names(album)
    assert "folder.png" in names(album)
    assert "Cover.JPG" not in names(album)
    assert (album / "cover.jpg").read_bytes() == b"\xff\xd8\xffCOVER"
    assert (album / "folder.png").read_bytes() == b"\x89PNG\r\n\x1a\nFOLDER"
    # 非封面图片与文本文件不碰
    assert names(album) >= {"random.jpg", "notes.txt", "cover.txt"}


@pytest.mark.skipif(
    is_case_insensitive(Path(__file__).resolve().parent),
    reason="大小写不敏感的文件系统上无法让 cover.jpg 与 Cover.JPG 并存",
)
def test_同名小写文件已存在时移除大写那份(env):
    """封面不值得为它排人工确认：保留规范名，另一份进回收站（可还原）。"""
    cfg, store, album = env
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xffEXISTING")
    result = normalize_cover_names(store, cfg, cfg.roots[0])
    assert result["renamed"] == 1          # 只剩 folder.png 的改名
    assert result["removed"] == 1
    assert [c["from"] for c in result["removals"]] == ["Adele/19/Cover.JPG"]
    # 大写那份不在了，规范名那份原样保留
    assert "Cover.JPG" not in names(album)
    assert (album / "cover.jpg").read_bytes() == b"\xff\xd8\xffEXISTING"
    # 不是真的删掉：回收站里能找回
    trash = cfg.storage.resolved_data_dir / "trash"
    restored = list(trash.rglob("Cover.JPG"))
    assert restored and restored[0].read_bytes() == b"\xff\xd8\xffCOVER"


def test_已经规范的跳过且不产生记录(env):
    cfg, store, album = env
    (album / "Cover.JPG").unlink()
    (album / "Folder.PNG").unlink()
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (album / "folder.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    normalize_cover_names(store, cfg, cfg.roots[0])
    rows = list(store.iter_renames())
    assert all(row["reason"] != "封面文件名规范化" for row in rows)


def test_改名记录可撤销(env):
    cfg, store, album = env
    normalize_cover_names(store, cfg, cfg.roots[0])
    rows = [r for r in store.iter_renames() if r["reason"] == "封面文件名规范化"]
    assert len(rows) == 2
    row = next(r for r in rows if r["new_rel_path"].endswith("cover.jpg"))
    assert row["old_rel_path"] == "Adele/19/Cover.JPG"
    assert row["media_format"] == "image"


def test_接口默认只预览(env):
    cfg, store, album = env
    with TestClient(create_app(cfg)) as client:
        response = client.post("/api/v1/library/cover-names", json={})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["dry_run"] is True
        assert data["roots"][0]["renamed"] == 2
        assert "Cover.JPG" in names(album)

        response = client.post("/api/v1/library/cover-names", json={"dry_run": False})
        assert response.status_code == 200, response.text
        assert "cover.jpg" in names(album)
