"""把「名字与音频不一致」的 .lrc 改成音频同名。

## 为什么必须做

Lyra 自己认得异名 sidecar（`pair_sidecars` 按规范化名字配对），
**但播放器不认**——它们只找 `<音频同名>.lrc`。实际库里：

    01 童年時.flac              ← 音频（繁体）
    01 - 张国荣 - 童年时.lrc     ← 下载来的歌词（简体 + 艺人前缀）

同一首歌，Lyra 能配上，换到车机上就是「没有歌词」。所以要么让名字统一，
要么等于白存。第二条：如果音频**没有**任何 lrc，而目录里有一份名字对不上
的歌词，应当把那份改名为音频同名（而不是报「找不到归属」）。

安全边界：
- 只改已配对成功的那份；目标名已被占用就跳过（那是两份歌词，该由人决定）
- 内容一字不动，只 rename
- 依据只有名字（含繁简折叠），不猜内容
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import repair_sidecar_names, sync_state

_LRC = "[ti:童年时]\n[ar:张国荣]\n[00:01.00]童年时 我与你家乡中相见天未亮\n"


@pytest.fixture
def env(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    album = lib / "張國榮" / "Salute"
    flac_factory(album / "01 童年時.flac", entries=(("TITLE", "童年時"),))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
    store.close()
    return cfg, lib / "張國榮" / "Salute"


def _repair(cfg, **kwargs):
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        return repair_sidecar_names(store, cfg, cfg.roots[0], **kwargs)
    finally:
        store.close()


def test_异名_lrc_改成音频同名(env):
    """繁简 + 艺人前缀都能认出来，然后改名为音频同名。"""
    cfg, album = env
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")

    out = _repair(cfg)
    assert out["renamed"] == 1, out
    assert (album / "01 童年時.lrc").is_file(), "应改成音频同名"
    assert not (album / "01 - 张国荣 - 童年时.lrc").exists()
    # 内容一字不动
    assert (album / "01 童年時.lrc").read_text(encoding="utf-8") == _LRC


def test_目标名已被占用时不动(env):
    """目录里已经有一份 <音频同名>.lrc —— 那是两份歌词，由人决定。"""
    cfg, album = env
    (album / "01 童年時.lrc").write_text("[00:02.00]另一份歌词\n", encoding="utf-8")
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")

    out = _repair(cfg)
    assert out["renamed"] == 0, out
    assert (album / "01 - 张国荣 - 童年时.lrc").is_file(), "不该动别人的文件"
    assert "另一份歌词" in (album / "01 童年時.lrc").read_text(encoding="utf-8")


def test_预演不改文件(env):
    cfg, album = env
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")
    out = _repair(cfg, dry_run=True)
    assert out["renamed"] == 1 and out["items"][0]["to"].endswith("01 童年時.lrc")
    assert (album / "01 - 张国荣 - 童年时.lrc").is_file(), "预演不得动文件"


def test_只写了曲名的_lrc_也能改成音频同名(env):
    """音频没有任何 .lrc，而目录里有一份只写了曲名的歌词。

    实际：归一化之前这类会被报成「unmatched（找不到归属）」，而它其实
    就该叫 <音频同名>.lrc——内容都不用动。
    """
    cfg, album = env
    (album / "童年时.lrc").write_text(_LRC, encoding="utf-8")
    out = _repair(cfg)
    assert out["renamed"] == 1, out
    assert (album / "01 童年時.lrc").is_file()


def test_接口一次处理两类(env):
    cfg, album = env
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")
    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/orphans/repair", json={"root": "lib"})
        assert r.status_code == 200, r.text
        entry = r.json()["roots"][0]
    assert entry["renamed"] == 1, entry
    assert (album / "01 童年時.lrc").is_file()


def test_接口能报出真实配对的_sidecar_与期望名(env):
    """修复面板要靠这个接口拿**真实**文件名。

    音频 `01 童年時.flac` 配到的是 `01 - 张国荣 - 童年时.lrc`；
    如果界面上按 `<音频同名>.lrc` 去猜，预览与改名都会落到不存在的路径。
    """
    cfg, album = env
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")

    with TestClient(create_app(cfg)) as client:
        d = client.get("/api/v1/files/sidecar", params={
            "root": "lib", "path": "張國榮/Salute/01 童年時.flac"}).json()

    assert d["exists"] is True
    assert d["path"] == "張國榮/Salute/01 - 张国荣 - 童年时.lrc"
    assert d["name_differs"] is True
    assert d["wanted"] == "張國榮/Salute/01 童年時.lrc"


def test_用孤儿歌词替换现有_lrc_旧文件进回收站(env):
    """同一首歌两份歌词、内容不同：用这份替换，旧的那份移入回收站。

    实际来源：下载来的 `.lrc` 与「从内嵌歌词生成的 `.lrc`」并存。
    旧文件不能直接删——它是用户的东西，要能还原。
    """
    cfg, album = env
    audio_rel = "張國榮/Salute/01 童年時.flac"
    (album / "01 童年時.lrc").write_text("[00:02.00]生成的另一份\n", encoding="utf-8")
    (album / "01 - 张国荣 - 童年时.lrc").write_text(_LRC, encoding="utf-8")

    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/orphans/replace", json={
            "root": "lib", "path": "張國榮/Salute/01 - 张国荣 - 童年时.lrc",
            "target": audio_rel, "confirm": True,
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["new_sidecar"] == "張國榮/Salute/01 童年時.lrc"
        assert d["replaced"], "旧 .lrc 必须进回收站"

    assert "童年时 我与你家乡中相见天未亮" in (
        (album / "01 童年時.lrc").read_text(encoding="utf-8"))
    assert not (album / "01 - 张国荣 - 童年时.lrc").exists()


def test_替换必须显式确认(env):
    cfg, album = env
    (album / "01 童年時.lrc").write_text("[00:02.00]x\n", encoding="utf-8")
    (album / "孤儿.lrc").write_text(_LRC, encoding="utf-8")
    with TestClient(create_app(cfg)) as client:
        r = client.post("/api/v1/orphans/replace", json={
            "root": "lib", "path": "張國榮/Salute/孤儿.lrc"})
    assert r.status_code == 400
