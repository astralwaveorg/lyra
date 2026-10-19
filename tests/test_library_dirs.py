"""「按目录刮削」的两块地基：目录列表与目录范围展开。

## 为什么必须测

1. **目录列表读磁盘，不读状态库。** 刚拷进来的目录还没被扫描登记，
   状态库里查不到——而「按目录刮削」恰恰最常用在刚拷完的时候。
   数字按层累加（父目录包含全部子孙），界面上才敢说「这个目录缺 12 个 lrc」。
2. **目录范围必须真的把范围限制住。** 传 `dir=周杰倫` 却扫了整库，
   使用者会以为「只刮了周杰伦」，实际已经把别的目录重刮一遍——
   慢、浪费上游额度，还可能把人工调过的歌重新判一遍。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lyra.providers as providers_module
from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


def _cfg(tmp_path: Path, lib: Path) -> Config:
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


@pytest.fixture
def lib(tmp_path: Path, flac_factory) -> Path:
    base = tmp_path / "lib"
    flac_factory(base / "周杰倫" / "范特西" / "01 爱在西元前.flac",
                 entries=(("TITLE", "爱在西元前"),))
    flac_factory(base / "周杰倫" / "范特西" / "02 简单爱.flac",
                 entries=(("TITLE", "简单爱"),))
    # 只有第一首有 .lrc
    (base / "周杰倫" / "范特西" / "01 爱在西元前.lrc").write_text(
        "[00:01.00]词\n", encoding="utf-8")
    flac_factory(base / "邓紫棋" / "新的心跳" / "01 多远都要在一起.flac",
                 entries=(("TITLE", "多远都要在一起"),))
    return base


def test_目录列表按层累加音频数与缺lrc数(lib, tmp_path):
    with TestClient(create_app(_cfg(tmp_path, lib))) as client:
        r = client.get("/api/v1/library/dirs")
    assert r.status_code == 200, r.text
    dirs = {d["path"]: d for d in r.json()["roots"][0]["dirs"]}

    assert dirs[""]["audio"] == 3 and dirs[""]["missing_lrc"] == 2
    assert dirs["周杰倫"]["audio"] == 2 and dirs["周杰倫"]["missing_lrc"] == 1
    assert dirs["周杰倫/范特西"]["audio"] == 2
    assert dirs["邓紫棋"]["audio"] == 1 and dirs["邓紫棋"]["missing_lrc"] == 1
    # 层级要带上，前端靠它做缩进
    assert dirs["周杰倫"]["depth"] == 1
    assert dirs["周杰倫/范特西"]["depth"] == 2
    assert dirs[""]["depth"] == 0


def _wait(client: TestClient, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/v1/tasks/{task_id}").json()
        if d["state"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_按目录刮削只处理该目录(lib, tmp_path, monkeypatch):
    """范围必须真的收窄：传 dir 就只扫这个目录下的音频。"""
    # 不联网：来源列表为空，run_fetch 只走「审计 → 同步」这一半
    monkeypatch.setattr(providers_module, "create_providers", lambda *a, **k: [])

    with TestClient(create_app(_cfg(tmp_path, lib))) as client:
        started = client.post("/api/v1/tasks/fetch",
                              json={"dir": "周杰倫", "apply": False}).json()
        done = _wait(client, started["id"])
    assert done["state"] == "done", done
    entry = done["result"]["roots"][0]
    assert entry["dir"] == "周杰倫"
    assert entry["scanned"] == 2, entry


def test_目录不存在时报错而不是扫全库(lib, tmp_path):
    with TestClient(create_app(_cfg(tmp_path, lib))) as client:
        started = client.post("/api/v1/tasks/fetch",
                              json={"dir": "不存在的目录", "apply": False}).json()
        done = _wait(client, started["id"])
    assert done["state"] == "failed", done
    assert "目录不存在" in (done["error"] or "")


def test_目录参数不能跳出受管根(lib, tmp_path):
    with TestClient(create_app(_cfg(tmp_path, lib))) as client:
        r = client.post("/api/v1/tasks/fetch", json={"dir": "../..", "apply": False})
    assert r.status_code == 400
