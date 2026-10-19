"""刮削任务的「歌词现状」筛选。

实际使用中最常问的是「哪些歌还没有歌词 / 只有纯文本 / 没有 .lrc」，
而不是「把整库再跑一遍」。这里固定四类条件的判定与组合方式：

- `none`      内嵌与外挂都没有歌词
- `unsynced`  有歌词，但没有任何一侧带时间轴
- `missing_sidecar`   没有 .lrc 文件（空文件算「有」）
- `missing_embedded`  没有内嵌歌词
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.api import _filter_by_lyrics
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole


@pytest.fixture
def env(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False
    )

    def add(rel: str, *, embedded: str, sidecar: str | None) -> None:
        loc = store.upsert_location(root_id=root_id, rel_path=rel, size=1, mtime=1.0)
        store.record_lyric_observation(
            location_id=loc,
            embedded_state=embedded,
            sidecar_state=sidecar,
        )

    add("both-synced.flac", embedded="synced", sidecar="synced")
    add("nothing.flac", embedded="absent", sidecar=None)
    add("sidecar-plain.flac", embedded="absent", sidecar="plain_text")
    add("embedded-only.flac", embedded="synced", sidecar=None)
    add("empty-sidecar.flac", embedded="absent", sidecar="absent")
    add("suspicious.flac", embedded="synced_suspicious", sidecar=None)
    yield cfg, store, root_id
    store.close()


def _pick(store, root_id, **kwargs) -> set[str]:
    return _filter_by_lyrics(store, root_id, **{
        "lyric_state": "", "missing_sidecar": False, "missing_embedded": False, **kwargs,
    })


def test_没有歌词(env):
    _cfg, store, root_id = env
    assert _pick(store, root_id, lyric_state="none") == {
        "nothing.flac", "empty-sidecar.flac",
    }


def test_没有时间轴(env):
    _cfg, store, root_id = env
    # 空的 .lrc 没有任何歌词，属于「无歌词」，不算「有词但没时间轴」
    assert _pick(store, root_id, lyric_state="unsynced") == {
        "sidecar-plain.flac", "suspicious.flac",
    }


def test_缺lrc文件(env):
    _cfg, store, root_id = env
    # 空的 .lrc 也算「有文件」，不算缺失
    assert _pick(store, root_id, missing_sidecar=True) == {
        "nothing.flac", "embedded-only.flac", "suspicious.flac",
    }


def test_缺内嵌歌词(env):
    _cfg, store, root_id = env
    assert _pick(store, root_id, missing_embedded=True) == {
        "nothing.flac", "sidecar-plain.flac", "empty-sidecar.flac",
    }


def test_两个缺失项之间是或(env):
    """同时勾选「缺 .lrc」和「缺内嵌」＝缺任意一侧都算，不是「两者都缺」。"""
    _cfg, store, root_id = env
    sidecar_only = _pick(store, root_id, missing_sidecar=True)
    embedded_only = _pick(store, root_id, missing_embedded=True)
    both = _pick(store, root_id, missing_sidecar=True, missing_embedded=True)
    assert both == sidecar_only | embedded_only
    assert both != sidecar_only & embedded_only


def test_状态与缺失项之间是与(env):
    _cfg, store, root_id = env
    assert _pick(
        store, root_id, lyric_state="unsynced", missing_embedded=True,
    ) == {"sidecar-plain.flac"}


def test_不选任何条件返回全部(env):
    _cfg, store, root_id = env
    assert len(_pick(store, root_id)) == 6


def test_接口拒绝未知的歌词状态(tmp_path: Path):
    from fastapi.testclient import TestClient

    from lyra.api import create_app

    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/api/v1/tasks/fetch", json={"filters": {"lyric_state": "whatever"}}
        )
    assert response.status_code == 400
