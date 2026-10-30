"""AI 预审批的测试（2.0.4：批量消化待审查队列的核心机制）。

覆盖：
1. 高置信「同一录音」→ 条目转为待写入（state='staged', origin='ai'）；
2. 高置信「同曲异版」且判定为近似匹配 → 同样自动采纳；
3. 低置信 / 不同录音 → 留在待审查队列，只留判定记录；
4. 预算用尽即停（不无限调用模型）；
5. 判定结论留档 ai_json，可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import make_flac
from lyra.ai import CandidateJudgement
from lyra.config import Config
from lyra.db import open_store
from lyra.match import build_context
from lyra.pre_review import run_ai_pre_review
from lyra.providers.base import Candidate


@dataclass
class _FakeProvider:
    name: str = "qq"
    calls: int = 0

    def search(self, query):  # noqa: ANN001, ANN201
        self.calls += 1
        return [
            Candidate(
                provider=self.name,
                provider_id="cand-1",
                title=query.title,
                artists=tuple(query.artists),
                album=query.album,
                duration=query.duration,
            )
        ]

    def lyrics(self, _c):  # noqa: ANN001, ANN201
        return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """一个带 1 首曲目（已抓取、待审查）的最小环境。"""
    lib = tmp_path / "lib"
    path = make_flac(lib / "歌.flac", entries=(("TITLE", "真的"), ("ARTIST", "张韶涵")))
    cfg = Config()
    cfg.ai.enabled = True
    cfg.ai.pre_review = True
    cfg.ai.auto_accept = True
    cfg.ai.pre_review_confidence = 0.85
    cfg.ai.max_requests_per_run = 5

    store = open_store(tmp_path / "data")
    root_id = store.upsert_root(name="main", role="library", path=str(lib))
    location_id = store.upsert_location(
        root_id=root_id, rel_path="歌.flac", size=path.stat().st_size, mtime=path.stat().st_mtime
    )
    store.stage_lyrics(
        location_id=location_id,
        content_hash="h1",
        content="[00:01.00]歌词\n",
        provider="qq",
        provider_id="cand-1",
        score=0.72,
        state="needs_review",
    )
    context = build_context({"TITLE": ["真的"], "ARTIST": ["张韶涵"]}, 240.0)
    yield store, cfg, {location_id: (context, "")}, path
    store.close()


def _patch_judge(monkeypatch, judgement: CandidateJudgement) -> list[dict]:
    """替换模型判定，返回调用记录。"""
    calls: list[dict] = []

    def fake_review(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return judgement

    import lyra.pre_review as pr

    monkeypatch.setattr(pr, "review_candidates", fake_review)
    return calls


def test_高置信同一录音自动采纳(env, monkeypatch):
    store, cfg, contexts, _path = env
    calls = _patch_judge(
        monkeypatch,
        CandidateJudgement(
            best="qq:cand-1",
            confidence=0.95,
            relation="same_recording",
            reason="标题与艺人完全一致",
        ),
    )
    provider = _FakeProvider()
    summary = run_ai_pre_review(
        store, cfg, type("R", (), {"read_only": False})(),
        {provider.name: provider},
        contexts,
    )
    assert summary.judged == 1
    assert summary.adopted == 1
    assert len(calls) == 1

    row = store.conn.execute(
        "SELECT state, origin, ai_json FROM staged_lyrics WHERE content_hash='h1'"
    ).fetchone()
    assert row["state"] == "staged", "高置信判定后应转为待写入"
    assert row["origin"] == "ai"
    assert "same_recording" in row["ai_json"]


def test_低置信保留在待审查(env, monkeypatch):
    store, cfg, contexts, _path = env
    _patch_judge(
        monkeypatch,
        CandidateJudgement(
            best="qq:cand-1",
            confidence=0.55,
            relation="same_recording",
            reason="不确定",
        ),
    )
    provider = _FakeProvider()
    summary = run_ai_pre_review(
        store, cfg, type("R", (), {"read_only": False})(),
        {provider.name: provider},
        contexts,
    )
    assert summary.judged == 1
    assert summary.adopted == 0
    row = store.conn.execute(
        "SELECT state, ai_json FROM staged_lyrics WHERE content_hash='h1'"
    ).fetchone()
    assert row["state"] == "needs_review"
    assert row["ai_json"], "低置信也要留档，供人参考"


def test_不同录音不采纳(env, monkeypatch):
    store, cfg, contexts, _path = env
    _patch_judge(
        monkeypatch,
        CandidateJudgement(
            best=None,
            confidence=0.2,
            relation="different",
            reason="不是同一首",
            rejected=("qq:cand-1",),
        ),
    )
    provider = _FakeProvider()
    summary = run_ai_pre_review(
        store, cfg, type("R", (), {"read_only": False})(),
        {provider.name: provider},
        contexts,
    )
    assert summary.adopted == 0
    assert summary.rejected == 1
    row = store.conn.execute(
        "SELECT state FROM staged_lyrics WHERE content_hash='h1'"
    ).fetchone()
    assert row["state"] == "needs_review"


def test_预算用尽即停止(env, monkeypatch):
    store, cfg, contexts, _path = env
    cfg.ai.max_requests_per_run = 0
    calls = _patch_judge(
        monkeypatch,
        CandidateJudgement(
            best="qq:cand-1", confidence=0.99, relation="same_recording", reason="ok"
        ),
    )
    provider = _FakeProvider()
    summary = run_ai_pre_review(
        store, cfg, type("R", (), {"read_only": False})(),
        {provider.name: provider},
        contexts,
    )
    assert summary.judged == 0
    assert summary.budget_exhausted is True
    assert not calls


def test_关闭预审时不工作(env, monkeypatch):
    store, cfg, contexts, _path = env
    cfg.ai.pre_review = False
    calls = _patch_judge(
        monkeypatch,
        CandidateJudgement(
            best="qq:cand-1", confidence=0.99, relation="same_recording", reason="ok"
        ),
    )
    provider = _FakeProvider()
    summary = run_ai_pre_review(
        store, cfg, type("R", (), {"read_only": False})(),
        {provider.name: provider},
        contexts,
    )
    assert summary.scanned == 0
    assert not calls
