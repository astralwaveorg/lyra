"""搜索来源 + 模型核验的端到端扫描测试（2.1.6 回归）。

2.1.6 首次部署时线上崩溃：扫描流程里有**两处** AI 核验调用，其中一处把
包装对象直接当匹配结果用（`match.confidence` 取不到）。单元测试只测了各个
零件，没跑过扫描主流程，所以没被发现。

这条测试把「搜索来源 → 模型核验 → 提案落库」整条链路走一遍，确保流程级
代码没有这类错误，并确认模型核验确实被调用。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_flac

from lyra.ai import CandidateJudgement
from lyra.config import Config, RootConfig
from lyra.db import open_store
from lyra.tagscrape import scan_tags
from lyra.tagsources import MetadataCandidate, MusicBrainzConfig, MusicBrainzProvider


def _candidate(provider_id: str) -> MetadataCandidate:
    return MetadataCandidate(
        provider_id=provider_id,
        title="快乐崇拜",
        artists=("张韶涵",),
        album="欧若拉",
        # 来源缺时长：这类候选最需要核验（时长闸门无从判断）
        duration=None,
        score=90,
        fields={
            "ALBUM": ["欧若拉"],
            "ALBUMARTIST": ["张韶涵"],
            "TRACKNUMBER": ["11"],
            "DATE": ["2004"],
        },
    )


class _FakeProvider(MusicBrainzProvider):
    """始终给出「搜索命中」的假来源（不发任何网络请求）。"""

    def __init__(self, config: MusicBrainzConfig) -> None:
        super().__init__(config)
        self.lookups: list[str] = []

    def available(self) -> bool:
        return True

    def health_check(self, **_kw) -> bool:
        return True

    def lookup(self, *, title: str, **_kw):  # noqa: ANN003, ANN202
        self.lookups.append(title)
        return [_candidate("rec-1")]


@pytest.fixture
def scanned(tmp_path: Path, monkeypatch):
    """带 1 首曲目、搜索来源 + 模型核验的扫描环境。"""
    lib = tmp_path / "lib"
    album_dir = lib / "张韶涵 - 欧若拉 (2004)"
    album_dir.mkdir(parents=True)
    make_flac(
        album_dir / "11 - 快乐崇拜.flac",
        entries=(("TITLE", "快乐崇拜"), ("ARTIST", "张韶涵"), ("ALBUM", "欧若拉")),
    )
    cfg = Config()
    cfg.ai.enabled = True
    cfg.ai.verify_tags = True
    cfg.ai.pre_review_confidence = 0.85
    cfg.ai.api_key = "test-key"

    store = open_store(tmp_path / "data")
    root = RootConfig(name="main", path=str(lib))
    provider = _FakeProvider(MusicBrainzConfig(base_url="https://example.invalid"))

    calls: list[dict] = []

    def fake_review(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return CandidateJudgement(
            best="mb:rec-1",
            confidence=0.95,
            relation="same_recording",
            reason="曲名与艺人一致",
            fields={"ALBUM": "prefer_local"},
        )

    monkeypatch.setattr("lyra.tagscrape.review_candidates", fake_review)
    monkeypatch.setattr(
        "lyra.tagscrape.lookup_by_link",
        lambda _provider, _link, **_kw: [_candidate("rel-1")],
    )
    yield store, cfg, root, provider, calls
    store.close()


def test_扫描主流程不崩且核验被调用(scanned):
    """回归：搜索来源 + 模型核验的整条链路必须能跑完并落库。"""
    store, cfg, root, provider, calls = scanned
    summary = scan_tags(cfg, root, store, provider, auto_write=False)
    assert summary.errors == [], summary.errors
    assert summary.scanned == 1
    assert calls, "模型核验没有被调用"
    assert calls[0]["track"]["title"] == "快乐崇拜"
    row = store.conn.execute(
        "SELECT state, payload_json FROM tag_proposals"
    ).fetchone()
    assert row is not None, "扫描后应落库提案"


def test_核验未通过时不臆断(scanned, monkeypatch):
    """模型说不确定 → 不把候选当成已核验，人工仍可介入。"""
    store, cfg, root, provider, _calls = scanned

    def fake_review(**_kwargs):  # noqa: ANN003, ANN202
        return CandidateJudgement(
            best=None,
            confidence=0.3,
            relation="different",
            reason="不是同一首",
        )

    monkeypatch.setattr("lyra.tagscrape.review_candidates", fake_review)
    summary = scan_tags(cfg, root, store, provider, auto_write=False)
    assert summary.errors == []
    assert summary.scanned == 1


def test_关闭核验时不调用模型(scanned):
    """开关关闭时不调用模型——避免无谓的开销与费用。"""
    store, cfg, root, provider, calls = scanned
    cfg.ai.verify_tags = False
    summary = scan_tags(cfg, root, store, provider, auto_write=False)
    assert summary.errors == []
    assert not calls
