"""纯音乐自动落定：来源与标题都表明「没有歌词」时写入占位。

## 为什么需要这个测试

伴奏/纯音乐曲目在三个来源里永远查不到词——这不是抓取失败，是这首歌的
准确状态。旧行为是每轮重查、负缓存过期后再查，永远停留在「无歌词」，
待办里始终挂着几十首永远无法解决的条目。

新行为：标题含纯音乐标记（伴奏/instrumental/卡拉OK），或来源明确标记
instrumental=true，且确实拿不到同步歌词时，写入「纯音乐，请欣赏」占位。
占位走正常写入链路，观测状态变为 PLACEHOLDER（界面显示「纯音乐」、
统计不挂红灯），下一轮抓取视为已满足跳过。

风险对称性也要固定：**普通歌曲**（标题没有纯音乐标记）查不到词时
绝不能写占位——那会把「没查到」误标成「没有词」，错失以后补词的机会。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.pipeline import run_apply, run_fetch
from lyra.providers.base import Candidate


def _cfg(tmp_path: Path, rel: str, title: str) -> Config:
    lib = tmp_path / "lib"
    make_flac(lib / rel, entries=(("TITLE", title), ("ARTIST", "人")))
    cfg = Config(
        roots=[RootConfig(name="main", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.write_enabled = True
    return cfg


class _EmptyProvider:
    name = "qq"

    def search(self, _query):  # noqa: ANN001, ANN202
        return []

    def lyrics(self, _c):  # noqa: ANN001, ANN202
        return None

    def close(self) -> None:
        pass


class _InstrumentalProvider:
    """lrclib 形态：候选带 instrumental 标记，但没有可取的歌词。"""

    name = "lrclib"

    def search(self, _query):  # noqa: ANN001, ANN202
        return [
            Candidate(
                provider="lrclib", provider_id="1", title="夜曲",
                artists=("人",), duration=3.0, extra={"instrumental": "true"},
            ),
        ]

    def lyrics(self, _c):  # noqa: ANN001, ANN202
        return None

    def close(self) -> None:
        pass


def _staged(store):
    return store.conn.execute(
        "SELECT provider, state, origin, content FROM staged_lyrics"
    ).fetchall()


def test_标题纯音乐且无候选时写入占位(tmp_path: Path):
    cfg = _cfg(tmp_path, "夜曲 (Instrumental).flac", "夜曲 (Instrumental)")
    store = open_store(cfg.storage.resolved_data_dir)
    summary = run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    rows = _staged(store)
    assert summary.instrumental_placeholder == 1
    assert len(rows) == 1
    provider, state, origin, content = rows[0]
    assert provider == "placeholder"
    assert state == "staged", "占位走自动写入链路，不进待办"
    assert origin == "auto"
    assert "纯音乐" in content
    # 解析结论记 MATCHED：不是「没查到」，是「查到了答案」
    outcome = store.conn.execute(
        "SELECT outcome FROM resolutions WHERE provider='placeholder'"
    ).fetchone()
    assert outcome is not None and outcome["outcome"] == "matched"
    store.close()


def test_来源标记纯音乐且无同步歌词时写入占位(tmp_path: Path):
    cfg = _cfg(tmp_path, "夜曲.flac", "夜曲")
    store = open_store(cfg.storage.resolved_data_dir)
    summary = run_fetch(store, cfg, cfg.roots[0], [_InstrumentalProvider()])
    assert summary.instrumental_placeholder == 1
    assert summary.no_synced == 1
    rows = _staged(store)
    assert len(rows) == 1 and rows[0][0] == "placeholder"
    store.close()


def test_普通歌曲无候选不写占位(tmp_path: Path):
    """标题没有纯音乐标记时，「没查到」必须保持「没查到」。"""
    cfg = _cfg(tmp_path, "夜曲.flac", "夜曲")
    store = open_store(cfg.storage.resolved_data_dir)
    summary = run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    assert summary.not_found == 1
    assert summary.instrumental_placeholder == 0
    assert _staged(store) == []
    store.close()


def test_占位关闭时不写(tmp_path: Path):
    cfg = _cfg(tmp_path, "夜曲 (伴奏).flac", "夜曲 (伴奏)")
    cfg.policy.instrumental_placeholder = False
    store = open_store(cfg.storage.resolved_data_dir)
    summary = run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    assert summary.not_found == 1
    assert summary.instrumental_placeholder == 0
    assert _staged(store) == []
    store.close()


def test_写入占位后下一轮抓取跳过(tmp_path: Path):
    """占位是「已解决」而不是「待办」：写入后不再耗配额重查。"""
    rel = "夜曲 (Instrumental).flac"
    cfg = _cfg(tmp_path, rel, "夜曲 (Instrumental)")
    store = open_store(cfg.storage.resolved_data_dir)
    run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    run_apply(store, cfg, cfg.roots[0])

    # 第二轮：文件里已经是占位（观测状态 PLACEHOLDER），应直接跳过
    summary = run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    assert summary.skipped_satisfied == 1, "占位应视为已满足"
    assert summary.considered == 0
    # 也没有重复暂存
    assert len(_staged(store)) == 1
    store.close()


def test_占位走写入链路真正落盘(tmp_path: Path):
    rel = "夜曲 (Instrumental).flac"
    cfg = _cfg(tmp_path, rel, "夜曲 (Instrumental)")
    store = open_store(cfg.storage.resolved_data_dir)
    run_fetch(store, cfg, cfg.roots[0], [_EmptyProvider()])
    applied = run_apply(store, cfg, cfg.roots[0])
    assert applied.applied == 1, f"占位应被写入：{applied.errors}"
    sidecar = (cfg.roots[0].resolved_path / "夜曲 (Instrumental).lrc")
    assert sidecar.is_file(), "占位应同步写出 .lrc"
    text = sidecar.read_text(encoding="utf-8")
    assert "纯音乐" in text
    store.close()
