"""把内嵌歌词补成 .lrc：只补缺的，绝不覆盖有内容的。

## 为什么要有这一步

库里大量文件在整理时就把带时间轴的歌词写进了内嵌标签（Picard/发行方自带）。
抓取层看到「已有有效同步歌词」直接跳过（`skip_if_valid_synced`），
于是这些文件永远拿不到 `.lrc`——而播放器、车机只认外挂文件。
实际新增 176 首之后，抓取结果是「接受 0／写入 0」，磁盘上只有 15 个 .lrc，
界面却显示歌词覆盖 100%。

所以这条路径不是「再抓一遍」（内容已在文件里，重抓只会引入差异），
而是**把内嵌的那份原样落成外挂文件**：不联网、不改音频、字节零风险。

固定四条边界：
1. 没有 .lrc 时创建，内容来自文件自身；
2. **有内容的一律不覆盖**（纯文本歌词也算「有内容」）；
3. 0 字节/纯空白占位视为空，可以填；
4. 不改音频——字节必须完全一致。
"""

from __future__ import annotations

from pathlib import Path

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import fill_sidecars

_EMBEDDED = "[00:01.00]第一句\n[00:02.50]第二句\n[00:04.00]第三句\n"


def _env(tmp_path: Path, flac_factory, *, entries=None, lrc=None):
    lib = tmp_path / "lib"
    entries = entries if entries is not None else (
        ("TITLE", "测试曲"), ("ARTIST", "测试歌手"), ("ALBUM", "测试专辑"),
        ("LYRICS", _EMBEDDED),
    )
    audio = flac_factory(lib / "专辑" / "歌.flac", entries=entries)
    if lrc is not None:
        (lib / "专辑" / "歌.lrc").write_text(lrc, encoding="utf-8")
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    return cfg, store, lib, audio


def test_没有_lrc_时按内嵌内容创建(tmp_path, flac_factory):
    cfg, store, lib, audio = _env(tmp_path, flac_factory)
    before = audio.read_bytes()

    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 1, summary.to_dict()

    lrc = lib / "专辑" / "歌.lrc"
    assert lrc.is_file()
    text = lrc.read_text(encoding="utf-8")
    # 头部来自标签：没有头部的外挂歌词在播放器里看不出是哪首歌的
    assert text.startswith("[ti:测试曲]\n[ar:测试歌手]\n[al:测试专辑]\n")
    assert "[00:01.00]第一句" in text and "[00:04.00]第三句" in text
    # 不碰音频
    assert audio.read_bytes() == before, "补 .lrc 绝对不能改动音频字节"
    store.close()


def test_已有内容的_lrc_绝不覆盖(tmp_path, flac_factory):
    """别人写的纯文本歌词也是内容，不是「空的」。"""
    original = "这是我自己整理的歌词\n没有时间轴\n"
    cfg, store, _lib, _audio = _env(tmp_path, flac_factory, lrc=original)

    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 0
    assert summary.untimed == 1, summary.to_dict()
    assert (_lib / "专辑" / "歌.lrc").read_text(encoding="utf-8") == original
    store.close()


def test_零字节占位可以填(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(tmp_path, flac_factory, lrc="")
    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 1, summary.to_dict()
    assert "第一句" in (lib / "专辑" / "歌.lrc").read_text(encoding="utf-8")
    store.close()


def test_已经有合格_lrc_时不动(tmp_path, flac_factory):
    cfg, store, _lib, _audio = _env(
        tmp_path, flac_factory, lrc="[00:01.00]别的来源\n[00:02.00]另一句\n")
    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 0 and summary.already == 1, summary.to_dict()
    store.close()


def test_内嵌没有歌词时什么也不做(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(
        tmp_path, flac_factory, entries=(("TITLE", "只有标题"),))
    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 0 and summary.no_source == 1
    assert not (lib / "专辑" / "歌.lrc").exists()
    store.close()


def test_预演不写文件(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    summary = fill_sidecars(store, cfg, cfg.roots[0], dry_run=True)
    assert summary.filled == 1 and summary.dry_run is True
    assert not (lib / "专辑" / "歌.lrc").exists(), "预演不得写文件"
    store.close()


def test_策略关闭时不写(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    cfg.policy.fill_empty_lrc = False
    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 0
    assert summary.errors and "fill_empty_lrc" in summary.errors[0]
    assert not (lib / "专辑" / "歌.lrc").exists()
    store.close()


def test_写入关闭时不写(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    cfg.policy.write_enabled = False
    summary = fill_sidecars(store, cfg, cfg.roots[0])
    assert summary.filled == 0
    assert not (lib / "专辑" / "歌.lrc").exists()
    store.close()


def test_只处理指定的路径(tmp_path, flac_factory):
    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    flac_factory(lib / "专辑" / "另一首.flac", entries=(
        ("TITLE", "另一首"), ("LYRICS", _EMBEDDED)))
    summary = fill_sidecars(
        store, cfg, cfg.roots[0], only_rel_paths={"专辑/歌.flac"})
    assert summary.filled == 1
    assert (lib / "专辑" / "歌.lrc").is_file()
    assert not (lib / "专辑" / "另一首.lrc").exists(), "不该顺带处理别的文件"
    store.close()


def test_写完立刻刷新状态(tmp_path, flac_factory):
    """写完 .lrc 要当场反映到状态库，否则界面仍显示「缺 .lrc」30 秒。"""
    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    fill_sidecars(store, cfg, cfg.roots[0])
    row = store.conn.execute(
        "SELECT sidecar_state FROM lyric_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["sidecar_state"] == "synced", row
    store.close()


def test_统计里缺_lrc_不算已覆盖(tmp_path, flac_factory):
    """这是使用者看到的那个数字：只有内嵌、没有 .lrc，不能算覆盖。"""
    from lyra.stats import collect_stats

    cfg, store, lib, _audio = _env(tmp_path, flac_factory)
    stats = collect_stats(cfg.roots[0])
    assert stats.audio_total == 1
    assert stats.embedded_synced == 1
    assert stats.sidecar_timed == 0
    assert stats.sidecar_coverage_pct == 0.0, "缺 .lrc 就是没覆盖"
    assert stats.embedded_coverage_pct == 100.0
    assert stats.sidecar_missing_embedded == 1
    # 补上之后再算：这时才算覆盖
    fill_sidecars(store, cfg, cfg.roots[0])
    stats = collect_stats(cfg.roots[0])
    assert stats.sidecar_timed == 1
    assert stats.sidecar_coverage_pct == 100.0
    store.close()
