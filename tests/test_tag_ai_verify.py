"""标签搜索结果的模型核验（2.1.3：标签自动化的最后一块拼图）。

背景：MusicBrainz 的搜索核验要求标题 ≥0.99、艺人 ≥0.85、时长 ≤3s 且专辑
证据吻合——华语专辑几乎永远达不到，于是全库标签落进「待审查」。模型核验
把这类「证据不足但很可能对」的候选提升为已核验，让自动写入重新可用。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_flac

from lyra.ai import CandidateJudgement
from lyra.config import Config
from lyra.tagscrape import MatchResult, TrackSnapshot, _ai_verify_search_match


def _snap(tmp_path: Path) -> TrackSnapshot:
    path = make_flac(tmp_path / "a.flac", entries=(("TITLE", "快乐崇拜"), ("ARTIST", "张韶涵")))
    from lyra.formats import read_audio_meta

    meta = read_audio_meta(path, tmp_path)
    return TrackSnapshot(
        location_id=1,
        rel_path="a.flac",
        path=path,
        tags=dict(meta.tags),
        duration=240.0,
        media_format="flac",
        writable=True,
        dir=str(tmp_path),
        file_size=path.stat().st_size,
        file_mtime=int(path.stat().st_mtime),
        title="快乐崇拜",
        artists=("张韶涵",),
        album="欧若拉",
    )


def _match() -> MatchResult:
    return MatchResult(
        source="search",
        confidence=0.8,
        release_id="rel-1",
        recording_id="rec-1",
        album="欧若拉",
        duration_delta=0.5,
        fields={"ALBUM": ["欧若拉"], "TRACKNUMBER": ["11"]},
        extra={},
        notes=["标题搜索来源，请逐项确认"],
        candidates=[
            {
                "provider_id": "rec-1",
                "title": "快乐崇拜",
                "artists": ["张韶涵"],
                "album": "欧若拉",
                "duration": 240.5,
                "score": 0.8,
                "duration_delta": 0.5,
            }
        ],
        content_strength=0.7,
        verified=False,
        verification="证据不足",
    )


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(storage=__import__("lyra.config", fromlist=["StorageConfig"]).StorageConfig(
        data_dir=str(tmp_path / "data")
    ))
    cfg.ai.enabled = True
    cfg.ai.verify_tags = True
    cfg.ai.auto_accept = True
    cfg.ai.pre_review_confidence = 0.85
    cfg.ai.api_key = "test-key"
    return cfg


def test_关闭核验时不介入(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ai.verify_tags = False
    calls: list = []
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates", lambda **kw: calls.append(kw) or None
    )
    assert _ai_verify_search_match(cfg, _snap(tmp_path), _match()) is None
    assert not calls


def test_高置信核验后标记为已核验(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates",
        lambda **kw: CandidateJudgement(
            best="mb:rec-1",
            confidence=0.95,
            relation="same_recording",
            reason="曲名与艺人完全一致",
        ),
    )
    result = _ai_verify_search_match(cfg, _snap(tmp_path), _match())
    assert result is not None
    assert result.verified is True
    assert result.confidence >= 0.95
    assert result.content_strength == 1.0
    assert any("模型核验" in note for note in result.notes)


def test_低置信核验不改变判定(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates",
        lambda **kw: CandidateJudgement(
            best="mb:rec-1",
            confidence=0.5,
            relation="same_recording",
            reason="不确定",
        ),
    )
    assert _ai_verify_search_match(cfg, _snap(tmp_path), _match()) is None


def test_不同录音不核验(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates",
        lambda **kw: CandidateJudgement(
            best=None,
            confidence=0.2,
            relation="different",
            reason="不是同一首",
        ),
    )
    assert _ai_verify_search_match(cfg, _snap(tmp_path), _match()) is None


def test_模型不可用时保持原状(tmp_path: Path, monkeypatch):
    from lyra.ai import AIError

    cfg = _cfg(tmp_path)

    def boom(**_kw):  # noqa: ANN002, ANN003, ANN202
        raise AIError("连接失败")

    monkeypatch.setattr("lyra.tagscrape.review_candidates", boom)
    assert _ai_verify_search_match(cfg, _snap(tmp_path), _match()) is None


def test_已核验的结果不再重复核验(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    from dataclasses import replace

    match = replace(_match(), verified=True)
    calls: list = []
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates", lambda **kw: calls.append(kw) or None
    )
    assert _ai_verify_search_match(cfg, _snap(tmp_path), match) is None
    assert not calls


def test_没有密钥时不调用模型(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ai.api_key = ""
    calls: list = []
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates", lambda **kw: calls.append(kw) or None
    )
    assert _ai_verify_search_match(cfg, _snap(tmp_path), _match()) is None
    assert not calls
