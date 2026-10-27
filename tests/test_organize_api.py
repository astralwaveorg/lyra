"""曲库整理 / 重复检测的 HTTP 边界。

关键点：预览必须落库成一份计划，执行只能执行那一份；删除重复副本后
仍能从整理日志逐条撤销。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.dedupe import scan_duplicates
from lyra.models import RootRole


@pytest.fixture
def client(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    old = lib / "旧" / "x.flac"
    flac_factory(
        old,
        entries=(
            ("TITLE", "童年时"), ("ARTIST", "张国荣"),
            ("ALBUM", "Salute"), ("TRACKNUMBER", "1"),
        ),
    )
    old.with_suffix(".lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        c.cfg = cfg  # type: ignore[attr-defined]
        c.lib = lib  # type: ignore[attr-defined]
        yield c


def test_preview_execute_and_undo(client) -> None:
    plan = client.post(
        "/api/v1/organize/preview", json={"root": "lib"}
    ).json()
    assert plan["summary"]["move"] == 1
    assert (client.lib / "旧" / "x.flac").is_file()

    result = client.post(
        "/api/v1/organize/execute",
        json={"plan_id": plan["plan_id"], "dirs": ["旧"]},
    ).json()
    assert result["ok"], result
    target = client.lib / "张国荣" / "Salute" / "01 - 童年时.flac"
    assert target.is_file()
    assert target.with_suffix(".lrc").is_file()

    ops = client.get("/api/v1/organize/ops").json()["items"]
    move_ops = [op for op in ops if op["kind"] == "move"]
    assert move_ops
    undone = client.post(
        "/api/v1/organize/undo", json={"ids": [op["id"] for op in ops]}
    ).json()
    assert undone["ok"], undone
    assert (client.lib / "旧" / "x.flac").is_file()


def test_execute_plan_twice_is_rejected(client) -> None:
    plan = client.post("/api/v1/organize/preview", json={"root": "lib"}).json()
    first = client.post(
        "/api/v1/organize/execute", json={"plan_id": plan["plan_id"]}
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/organize/execute", json={"plan_id": plan["plan_id"]}
    )
    assert second.status_code == 409


def test_cleanup_preview_and_execute(client) -> None:
    (client.lib / "empty").mkdir()
    preview = client.post(
        "/api/v1/organize/cleanup-preview", json={"root": "lib"}
    ).json()
    paths = [item["rel_path"] for item in preview["items"]]
    assert "empty" in paths
    result = client.post(
        "/api/v1/organize/cleanup", json={"root": "lib", "paths": ["empty"]}
    ).json()
    assert result["ok"], result
    assert not (client.lib / "empty").exists()


def test_duplicate_groups_and_delete_undo(client, flac_factory) -> None:
    from lyra.db import open_store

    lib = client.lib
    flac_factory(
        lib / "副本" / "copy.flac",
        entries=(("TITLE", "童年时"), ("ARTIST", "张国荣"), ("ALBUM", "Salute")),
    )
    store = open_store(client.cfg.storage.resolved_data_dir)
    try:
        scan_duplicates(store, client.cfg, client.cfg.roots)
    finally:
        store.close()

    data = client.get("/api/v1/dedupe/groups").json()
    assert data["duplicates"] == 1
    group = data["groups"][0]
    assert group["kind"] == "exact"
    victim = next(
        item["rel_path"] for item in group["items"] if item["suggested_delete"]
    )
    result = client.post(
        f"/api/v1/dedupe/groups/{group['id']}/delete", json={"paths": [victim]}
    ).json()
    assert result["deleted"] == 1
    assert not (lib / victim).exists()
    assert client.get("/api/v1/dedupe/groups").json()["groups"] == []

    ops = client.get("/api/v1/organize/ops").json()["items"]
    assert ops and ops[0]["kind"] == "delete"
    undone = client.post(
        "/api/v1/organize/undo", json={"ids": [ops[0]["id"]]}
    ).json()
    assert undone["ok"], undone
    assert (lib / victim).is_file()


def test_dedupe_dismiss_survives_rescan_and_can_restore(
    client, flac_factory
) -> None:
    """忽略的组再扫一次也不该回来，恢复后重新出现在待处理列表。"""
    from lyra.db import open_store

    lib = client.lib
    flac_factory(
        lib / "副本" / "copy.flac",
        entries=(("TITLE", "童年时"), ("ARTIST", "张国荣"), ("ALBUM", "Salute")),
    )
    store = open_store(client.cfg.storage.resolved_data_dir)
    try:
        scan_duplicates(store, client.cfg, client.cfg.roots)
    finally:
        store.close()

    data = client.get("/api/v1/dedupe/groups").json()
    group = data["groups"][0]
    assert client.post(f"/api/v1/dedupe/groups/{group['id']}/dismiss").status_code == 200
    assert client.get("/api/v1/dedupe/groups").json()["groups"] == []

    store = open_store(client.cfg.storage.resolved_data_dir)
    try:
        scan_duplicates(store, client.cfg, client.cfg.roots)
    finally:
        store.close()
    after = client.get("/api/v1/dedupe/groups").json()
    assert after["groups"] == [], "忽略的组不该在扫描后回来"
    assert len(after["dismissed"]) == 1

    dismissed = after["dismissed"][0]
    assert client.post(f"/api/v1/dedupe/groups/{dismissed['id']}/restore").status_code == 200
    assert len(client.get("/api/v1/dedupe/groups").json()["groups"]) == 1


def test_dedupe_delete_can_target_the_recommended_keep(client, flac_factory) -> None:
    """人工改选：建议保留的那一份也能删，判错了要能改。"""
    from lyra.db import open_store

    lib = client.lib
    flac_factory(
        lib / "副本" / "copy.flac",
        entries=(("TITLE", "童年时"), ("ARTIST", "张国荣"), ("ALBUM", "Salute")),
    )
    store = open_store(client.cfg.storage.resolved_data_dir)
    try:
        scan_duplicates(store, client.cfg, client.cfg.roots)
    finally:
        store.close()

    group = client.get("/api/v1/dedupe/groups").json()["groups"][0]
    keep = group["keep_rel_path"]
    result = client.post(
        "/api/v1/dedupe/delete",
        json={"items": [{"group_id": group["id"], "path": keep}]},
    ).json()
    assert result["deleted"] == 1, result
    assert not (lib / keep).exists()


def test_dedupe_bulk_delete_by_kind_is_scoped(client, flac_factory) -> None:
    """按分类一键删除只处理该分类，参照项与其他分类不受影响。"""
    from lyra.db import open_store
    from lyra.dedupe import scan_duplicates

    lib = client.lib
    copy = flac_factory(
        lib / "副本" / "copy.flac",
        entries=(("TITLE", "童年时"), ("ARTIST", "张国荣"), ("ALBUM", "Salute"),
                 ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    copy.with_suffix(".lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    flac_factory(
        lib / "另一版" / "other.flac",
        entries=(("TITLE", "童年时"), ("ARTIST", "张国荣"), ("ALBUM", "Salute"),
                 ("MUSICBRAINZ_TRACKID", "rec-1")),
        audio=b"\xfc" * 256,
    )
    store = open_store(client.cfg.storage.resolved_data_dir)
    try:
        scan_duplicates(store, client.cfg, client.cfg.roots)
    finally:
        store.close()
    groups = client.get("/api/v1/dedupe/groups").json()["groups"]
    assert next(group for group in groups if group["kind"] == "exact")
    recording = next(group for group in groups if group["kind"] == "recording")
    assert any(item.get("reference") for item in recording["items"])

    response = client.post("/api/v1/dedupe/delete", json={"kind": "exact"})
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1

    remaining = client.get("/api/v1/dedupe/groups").json()["groups"]
    assert not [group for group in remaining if group["kind"] == "exact"]
    assert [group for group in remaining if group["kind"] == "recording"]
