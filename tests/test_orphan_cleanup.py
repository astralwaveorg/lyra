"""自动清理「确实多余」的孤儿 .lrc。

判定分三档，只有前两档动文件：

1. `duplicate` 且孤儿那份的**歌词行** 100% 被现有 .lrc 覆盖 → 内容重复
2. `unmatched` 且**整个曲库**没有任何音频名字与它相关 → 没有对应音频
3. 其余（多候选歧义、内容有差异）→ 不动

动作是移入回收站（保留相对路径、可还原），不是删除。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import clean_orphan_sidecars, sync_state

_LRC = "[00:01.00]童年时 我与你家乡中相见天未亮\n[00:02.00]你与我 永远心意也一样\n"


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
    return cfg, lib, album


def _run(cfg, **kwargs):
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        return clean_orphan_sidecars(store, cfg, cfg.roots[0], **kwargs)
    finally:
        store.close()


def test_没有对应音频的孤儿歌词被清理(env):
    """曲库里根本没有 `青空未来` 这首歌 → 这份 .lrc 是多余的。"""
    cfg, _lib, album = env
    stray = album / "五月天阿信-青空未来.lrc"
    stray.write_text(_LRC, encoding="utf-8")

    out = _run(cfg)
    assert out["cleaned"] == 1, out
    assert not stray.exists()
    # 进的是回收站，不是被删——要能还原
    assert out["trash_batch"]
    trashed = list((Path(cfg.storage.data_dir) / "trash").rglob("*.lrc"))
    assert len(trashed) == 1


def test_内容重复的孤儿被清理(env):
    """音频已有 .lrc，孤儿与它歌词行完全一致 → 纯重复。"""
    cfg, _lib, album = env
    (album / "01 童年時.lrc").write_text(_LRC, encoding="utf-8")
    orphan = album / "01 - 张国荣 - 童年时.lrc"
    orphan.write_text("[ti:童年时]\n" + _LRC, encoding="utf-8")

    out = _run(cfg)
    assert out["cleaned"] == 1, out
    assert not orphan.exists()
    assert (album / "01 童年時.lrc").exists(), "现有那份必须留着"


def test_多出真歌词行的孤儿不动(env):
    """孤儿有自己的歌词行 → 可能是不同版本，留着让人看。"""
    cfg, _lib, album = env
    (album / "01 童年時.lrc").write_text(_LRC, encoding="utf-8")
    orphan = album / "01 - 张国荣 - 童年时.lrc"
    orphan.write_text(_LRC + "[00:03.00]这一句现有那份没有\n", encoding="utf-8")

    out = _run(cfg)
    assert out["cleaned"] == 0, out
    assert orphan.exists()
    assert out["kept_items"][0]["why"].startswith("内容有差异")


def test_跨目录同名音频时不清理(env):
    """曲库别处有一首同名音频 → 可能是放错目录，不删。"""
    cfg, lib, album = env
    other = lib / "某合辑"
    other.mkdir()
    (other / "青空未来.flac").write_bytes((album / "01 童年時.flac").read_bytes())

    stray = album / "五月天阿信-青空未来.lrc"
    stray.write_text(_LRC, encoding="utf-8")

    out = _run(cfg)
    assert out["cleaned"] == 0, out
    assert stray.exists()
    assert "同名音频" in out["kept_items"][0]["why"]


def test_预演不动文件(env):
    cfg, _lib, album = env
    stray = album / "没人要的歌词.lrc"
    stray.write_text(_LRC, encoding="utf-8")
    out = _run(cfg, dry_run=True)
    assert out["cleaned"] == 1 and out["dry_run"] is True
    assert stray.exists()
