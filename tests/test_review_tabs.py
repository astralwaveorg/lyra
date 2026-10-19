"""「推荐采纳」与「待审查」必须是两个互不重叠的桶。

界面把歌词审查拆成三个标签：推荐采纳 / 待审查 / 无音源。
如果同一首歌既出现在「推荐采纳」又出现在「待审查」，使用者会以为有两份要处理，
批量采用之后还会在另一个标签里再看到它——这正是要守住的边界。

判据只有一份：`_safe_batch_entries`。列表接口的 `exclude_safe` 复用它，
这里从接口层面确认两个桶的并集等于全部待审查项、交集为空。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole


def _checks(*, artist_ok: bool) -> dict:
    checks = [
        {"key": "duration_gate", "label": "时长差", "passed": True, "detail": "差 0.1s"},
        {"key": "version", "label": "版本证据", "passed": True, "detail": "一致"},
        {"key": "title", "label": "标题相似度", "passed": True, "detail": "0.99"},
        {"key": "artist", "label": "艺人相似度", "passed": artist_ok, "detail": "0.95"},
        {"key": "confidence", "label": "匹配总分", "passed": False, "detail": "0.62"},
    ]
    return {
        "recommendation": {"verdict": "inspect", "text": "仅总分不足"},
        "checks": checks,
    }


def _seed(store, root_id: int, rel: str) -> tuple[int, int]:
    loc = store.upsert_location(root_id=root_id, rel_path=rel, size=1, mtime=1.0)
    now = time.time()
    ids = {}
    for tag, provider_id, artist_ok in (("safe", "safe-1", True), ("unsafe", "unsafe-1", False)):
        staged = store.stage_lyrics(
            location_id=loc, content_hash=f"hash-{tag}", content="[00:01.00]词\n",
            provider="qq", provider_id=provider_id, score=0.62,
            duration_delta=0.1, state="needs_review",
        )
        store.conn.execute(
            "INSERT INTO resolutions(resolution_key, provider, provider_id, outcome, "
            "candidate_json, location_id, attempts, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f"key-{rel}-{tag}", "qq", provider_id, "needs_review",
             json.dumps(_checks(artist_ok=artist_ok), ensure_ascii=False),
             loc, 1, now, now),
        )
        ids[tag] = staged
    store.conn.commit()
    return ids["safe"], ids["unsafe"]


@pytest.fixture
def seeded(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    for name in ("a.flac", "b.flac"):
        flac_factory(lib / name, entries=(("TITLE", name), ("ARTIST", "X")))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False
    )
    safe_id, unsafe_id = _seed(store, root_id, "a.flac")
    _seed(store, root_id, "b.flac")  # b 的两条：一安全、一不安全；按曲目只算一首
    store.close()
    return cfg, safe_id, unsafe_id


def test_推荐项与待审查项互不重叠(seeded):
    cfg, safe_id, unsafe_id = seeded
    with TestClient(create_app(cfg)) as client:
        safe = client.get("/api/v1/staged/safe-batch").json()
        all_rows = client.get(
            "/api/v1/staged", params={"state": "needs_review"}
        ).json()
        pending = client.get(
            "/api/v1/staged", params={"state": "needs_review", "exclude_safe": "true"}
        ).json()

    safe_ids = {item["id"] for item in safe["items"]}
    pending_ids = {item["id"] for item in pending["items"]}
    all_ids = {item["id"] for item in all_rows["items"]}

    assert safe_ids, "夹具里至少应有一项被判为推荐采纳"
    assert not (safe_ids & pending_ids), "同一项同时出现在两个标签里"
    assert safe_ids | pending_ids == all_ids, "两个标签加起来应覆盖全部待审查项"
    assert safe_id in safe_ids
    assert unsafe_id in pending_ids


def test_推荐项带上查看歌词需要的字段(seeded):
    """前端的「查看」要按 root + path + 来源取歌词，缺一个都取不到。"""
    cfg, _safe_id, _unsafe_id = seeded
    with TestClient(create_app(cfg)) as client:
        item = client.get("/api/v1/staged/safe-batch").json()["items"][0]
    assert item["root"] == "lib"
    assert item["rel_path"]
    assert item["provider"] == "qq"
    assert item["provider_id"]


def test_批量采用接口注册在正确路径上(seeded):
    """`safe-batch/accept` 必须是一条真实路由。

    它曾经漏了装饰器，于是请求落到了 `/staged/{id}/accept`，
    `id="safe-batch"` 触发路径参数校验失败，界面只看到
    「请求参数不正确」——批量采用整个功能等于没有。
    """
    cfg, safe_id, _unsafe_id = seeded
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/api/v1/staged/safe-batch/accept", json={"ids": [safe_id]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 1
