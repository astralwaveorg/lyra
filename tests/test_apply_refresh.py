"""写入之后必须刷新「观测」，否则界面会一直显示写入前的状态。

## 为什么必须有

用户报的现象：手动刮削并写入成功，刷新页面还是「无歌词」，于是怀疑写入失败。

成因：界面（音乐库列表、问题分布）读的是 `lyric_observations` 里最新那一条，
而写观测的只有 `sync_state`（审计/抓取路径）。写入路径原来不回写，于是
文件里已经有歌词了，列表里还写着「无歌词」，要等监控进程下一次周期对账才自愈。

这份测试固定两件事：
1. `run_apply` 成功写入后，该曲目的观测会变成「有歌词」；
2. 写完当场（不经过监控进程）就能查到新状态。
"""

from __future__ import annotations

from pathlib import Path

from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import run_apply, sync_state


def _make(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    flac_factory(lib / "专辑" / "歌.flac", entries=(("TITLE", "歌"), ("ARTIST", "某人")))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
    return cfg, store, lib


def _observation(store, rel_path: str):
    return store.conn.execute(
        "SELECT obs.embedded_state, obs.sidecar_state, obs.observed_at "
        "FROM locations loc JOIN lyric_observations obs ON obs.location_id = loc.id "
        "WHERE loc.rel_path = ? ORDER BY obs.id DESC LIMIT 1",
        (rel_path,),
    ).fetchone()


def test_写入后观测立刻反映新状态(tmp_path, flac_factory):
    cfg, store, lib = _make(tmp_path, flac_factory)
    rel = "专辑/歌.flac"

    before = _observation(store, rel)
    assert before["embedded_state"] == "absent", "前提：一开始没有歌词"

    location_id = store.conn.execute(
        "SELECT id FROM locations WHERE rel_path=?", (rel,)
    ).fetchone()["id"]
    store.stage_lyrics(
        location_id=location_id, content_hash="h1",
        content="[00:01.00]词\n[00:02.00]曲\n",
        provider="qq", provider_id="x", state="staged",
    )

    summary = run_apply(store, cfg, cfg.roots[0], only_rel_paths={rel})
    assert summary.applied == 1, summary.errors

    after = _observation(store, rel)
    assert after["embedded_state"] == "synced" or after["sidecar_state"] == "synced", (
        f"写入之后观测仍是 {after['embedded_state']}/{after['sidecar_state']}，"
        "界面会一直显示「无歌词」"
    )
    assert after["observed_at"] > before["observed_at"], "应当写入了一条新的观测"
    store.close()


def test_没有写入时不产生多余观测(tmp_path, flac_factory):
    """没写任何东西时不该白跑一次审计——那只是浪费。"""
    cfg, store, _lib = _make(tmp_path, flac_factory)
    rel = "专辑/歌.flac"
    before = _observation(store, rel)
    summary = run_apply(store, cfg, cfg.roots[0], only_rel_paths={rel})
    assert summary.applied == 0
    after = _observation(store, rel)
    assert after["observed_at"] == before["observed_at"], "不该新增观测"
    store.close()
