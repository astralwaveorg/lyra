"""标签刮削：人工决定必须留得住。

## 为什么要单独测

之前的实现里人工痕迹会丢：

- 「链接指定」把提案写成 `source='link'`，但**写入完成后整行被删掉**，
  人工断言随之消失。下一轮扫描只要算出的匹配跟上次不同（标题搜索尤其
  不稳），这条曲子又回到待审查，看起来像「人工确认没用」。
- 「跳过」只有一个 `state='skipped'`，既看不见也恢复不了，等于没有
  「忽略」这个能力。

现在人工痕迹留在行上：`ignored`（人工忽略）、`done`（人工已定案）。
扫描认得它们，不得覆盖。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import lyra.api as api_module
from lyra.api import create_app
from lyra.db import open_store
from lyra.tagscrape import scan_tags
from lyra.tagsources import MusicBrainzConfig, MusicBrainzProvider
from tests.test_tag_scrape import (
    RELEASE,
    _config,
    _handler,
    _release_json,
)


def _provider(handler) -> MusicBrainzProvider:
    return MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def _make_env(tmp_path, flac_factory, monkeypatch, *, conflict: bool):
    """一个曲库、一条提案，外加一个可以改的「来源」。

    `mutable['date']` 用来模拟来源变了：同一首曲子，下一轮扫描算出的
    发行信息跟上次不同。真实场景里标题搜索结果经常这样飘。

    `conflict=True` 时本地厂牌与来源不一致 → 提案落在「待审查」。
    """
    library = tmp_path / "lib"
    entries = [("TITLE", "Song One"), ("ARTIST", "Test Artist")]
    if conflict:
        entries.append(("LABEL", "Local Label"))
    flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=tuple(entries),
    )
    cfg = _config(tmp_path, library)
    mutable = {"date": "2001-02-03"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/ws/2/release/{RELEASE}":
            data = _release_json()
            data["date"] = mutable["date"]
            return httpx.Response(200, json=data)
        return _handler(request)

    monkeypatch.setattr(
        api_module,
        "MusicBrainzProvider",
        lambda config, **kw: _provider(handler),
    )
    provider = _provider(handler)
    store = open_store(cfg.storage.resolved_data_dir)
    scan_tags(
        cfg, cfg.roots[0], store, provider,
        auto_write=False, progress=lambda _m: None,
    )
    store.close()
    client = TestClient(create_app(cfg))
    client.__enter__()
    return client, cfg, provider, mutable


@pytest.fixture
def env(tmp_path, flac_factory, monkeypatch):
    client, cfg, provider, mutable = _make_env(
        tmp_path, flac_factory, monkeypatch, conflict=False
    )
    yield client, cfg, provider, mutable
    client.__exit__(None, None, None)
    provider.close()


@pytest.fixture
def review_env(tmp_path, flac_factory, monkeypatch):
    """带一条「待审查」提案（厂牌冲突，需要人工判断）。"""
    client, cfg, provider, mutable = _make_env(
        tmp_path, flac_factory, monkeypatch, conflict=True
    )
    yield client, cfg, provider, mutable
    client.__exit__(None, None, None)
    provider.close()


def _rescan(cfg, provider) -> None:
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=False, progress=lambda _m: None,
        )
    finally:
        store.close()


def _state_of(cfg, proposal_id: int) -> str:
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        row = store.conn.execute(
            "SELECT state FROM tag_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return str(row["state"]) if row else ""
    finally:
        store.close()


def _queue_total(client) -> int:
    """还需要人看一眼的队列总数（已忽略 / 已定案都不在里面）。"""
    return client.get("/api/v1/tags/proposals").json()["total"]


def test_链接指定并写入完成后条目出队(env):
    client, cfg, _provider_, _mutable = env
    item = client.get("/api/v1/tags/proposals").json()["items"][0]

    linked = client.post(
        f"/api/v1/tags/proposals/{item['id']}/link",
        json={"url": f"https://musicbrainz.org/release/{RELEASE}", "index": 0},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["proposal"]["source"] == "link"

    written = client.post(f"/api/v1/tags/proposals/{item['id']}/apply", json={})
    assert written.status_code == 200, written.text
    assert written.json()["remaining"] == 0

    # 全部满足 → 不再是待办
    assert _queue_total(client) == 0
    assert _state_of(cfg, item["id"]) == "done"


def test_已定案的条目不会被下一轮扫描带回(env):
    """核心回归：人工确认过的曲子，来源再怎么变也不能回到队列。"""
    client, cfg, provider, mutable = env
    item = client.get("/api/v1/tags/proposals").json()["items"][0]
    client.post(
        f"/api/v1/tags/proposals/{item['id']}/link",
        json={"url": f"https://musicbrainz.org/release/{RELEASE}", "index": 0},
    )
    client.post(f"/api/v1/tags/proposals/{item['id']}/apply", json={})
    assert _state_of(cfg, item["id"]) == "done"

    # 来源变了：同一条曲子，下一轮算出的发行信息跟上次不同
    mutable["date"] = "2020-05-05"
    _rescan(cfg, provider)

    assert _state_of(cfg, item["id"]) == "done"
    assert _queue_total(client) == 0
    assert client.get("/api/v1/tags/proposals", params={"state": "review"}).json()["total"] == 0
    assert client.get("/api/v1/tags/proposals", params={"state": "unmatched"}).json()["total"] == 0


def test_忽略后不再出现在队列且扫描翻不动(review_env):
    client, cfg, provider, _mutable = review_env
    listing = client.get("/api/v1/tags/proposals", params={"state": "review"}).json()
    assert listing["total"] == 1
    item = listing["items"][0]

    ignored = client.post(f"/api/v1/tags/proposals/{item['id']}/ignore", json={})
    assert ignored.status_code == 200, ignored.text

    assert _queue_total(client) == 0
    assert client.get("/api/v1/tags/proposals", params={"state": "ignored"}).json()["total"] == 1

    _rescan(cfg, provider)
    assert _state_of(cfg, item["id"]) == "ignored"
    assert _queue_total(client) == 0


def test_已忽略可以恢复回队列(review_env):
    client, cfg, _provider_, _mutable = review_env
    item = client.get("/api/v1/tags/proposals", params={"state": "review"}).json()["items"][0]
    client.post(f"/api/v1/tags/proposals/{item['id']}/ignore", json={})

    summary = client.get("/api/v1/tags/summary").json()
    assert summary["counts"]["ignored"] == 1
    # 已忽略不算待办
    assert summary["counts"]["review"] == 0
    assert summary["total"] == (
        summary["counts"]["auto"]
        + summary["counts"]["review"]
        + summary["counts"]["unmatched"]
    )

    restored = client.post(f"/api/v1/tags/proposals/{item['id']}/restore", json={})
    assert restored.status_code == 200, restored.text
    assert _state_of(cfg, item["id"]) == "review"
    assert client.get("/api/v1/tags/proposals", params={"state": "review"}).json()["total"] == 1
    assert client.get("/api/v1/tags/proposals", params={"state": "ignored"}).json()["total"] == 0


def test_恢复一条没有忽略的提案会被拒绝(review_env):
    client, _cfg, _provider_, _mutable = review_env
    item = client.get("/api/v1/tags/proposals", params={"state": "review"}).json()["items"][0]
    assert client.post(f"/api/v1/tags/proposals/{item['id']}/restore", json={}).status_code == 400


def test_自动写入不留已定案记录(env):
    """自动写入没有人工意图：行照旧删掉，别把表撑起来。"""
    client, cfg, provider, _mutable = env
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=True, progress=lambda _m: None,
        )
        assert summary.written == 1, summary.to_dict()
        left = store.conn.execute("SELECT COUNT(*) c FROM tag_proposals").fetchone()["c"]
        assert left == 0, "自动写入后不该留下提案行"
    finally:
        store.close()


def test_升级后库里多出忽略时间列(tmp_path):
    """迁移 14 给 tag_proposals 加了 dismissed_at。"""
    from lyra.db import SCHEMA_VERSION, open_store

    store = open_store(tmp_path / "data")
    try:
        assert store.current_schema_version() == SCHEMA_VERSION
        columns = {
            str(row["name"]) for row in store.conn.execute("PRAGMA table_info(tag_proposals)")
        }
        assert "dismissed_at" in columns
    finally:
        store.close()
