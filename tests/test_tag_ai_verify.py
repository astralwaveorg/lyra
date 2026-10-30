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
    assert result.match.verified is True
    assert result.match.confidence >= 0.95
    assert result.match.content_strength == 1.0
    assert any("模型核验" in note for note in result.match.notes)


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


# ---------------------------------------------------------------------------
# 字段级判定：消化「不同发行版本」造成的冲突
# ---------------------------------------------------------------------------


def _conflict_rows():
    """一个典型的冲突：本地是具体再版名，来源是主专辑名；另有发行日期差异。"""
    return [
        {"key": "ALBUM", "current": ["Yellow Submarine (MFSL SuperVinyl)"],
         "suggested": ["The Beatles"], "action": "conflict"},
        {"key": "DATE", "current": ["1969"], "suggested": ["1988"], "action": "conflict"},
        {"key": "ARTIST", "current": ["Beatles, The"], "suggested": ["The Beatles"],
         "action": "conflict"},
    ]


def test_本地更准则保留本地且不算冲突():
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(
        _conflict_rows(), {"ALBUM": "prefer_local", "DATE": "prefer_local"}
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["ALBUM"]["action"] == "fill"
    assert by_key["ALBUM"]["suggested"] == ["Yellow Submarine (MFSL SuperVinyl)"]
    assert by_key["DATE"]["action"] == "fill"


def test_来源更准则自动纠正():
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(
        _conflict_rows(), {"ALBUM": "prefer_source", "DATE": "prefer_source"}
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["ALBUM"]["action"] == "correct"
    assert by_key["ALBUM"]["suggested"] == ["The Beatles"]


def test_等价写法自动纠正():
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(_conflict_rows(), {"ALBUM": "equivalent"})
    by_key = {row["key"]: row for row in rows}
    assert by_key["ALBUM"]["action"] == "correct"


def test_关键字段即使模型说来源更准也留给人工():
    """曲名/艺人写错的代价远高于多问一次——关键字段只允许「等价」自动处理。"""
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(
        _conflict_rows(), {"ARTIST": "prefer_source", "ALBUM": "prefer_source"}
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["ARTIST"]["action"] == "conflict", "艺人字段必须留给人工"
    assert by_key["ALBUM"]["action"] == "correct"


def test_判断不了的字段保持冲突():
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(
        _conflict_rows(), {"ALBUM": "uncertain", "DATE": "uncertain"}
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["ALBUM"]["action"] == "conflict"
    assert by_key["DATE"]["action"] == "conflict"


def test_没有判定的字段不受影响():
    from lyra.tagscrape import _apply_field_decisions

    rows = _apply_field_decisions(_conflict_rows(), {})
    assert all(row["action"] == "conflict" for row in rows)


# ---------------------------------------------------------------------------
# 歌词来源兜底：MusicBrainz 覆盖不到时用 QQ / 网易云提供专辑候选
# ---------------------------------------------------------------------------


def test_歌词来源候选转成可写入字段():
    from lyra.tagscrape import _lyrics_source_fields

    fields = _lyrics_source_fields({
        "provider": "qq", "provider_id": "x",
        "title": "快乐崇拜", "artists": ["张韶涵"], "album": "欧若拉",
    })
    assert fields["ALBUM"] == ["欧若拉"]
    assert fields["ARTIST"] == ["张韶涵"]


def test_歌词来源核验通过才生成冲突行(tmp_path: Path, monkeypatch):
    """模型确认是同一首，才允许按来源改写专辑名。"""
    from lyra.tagscrape import _lyrics_source_rows

    cfg = _cfg(tmp_path)
    snap = _snap(tmp_path)
    candidates = [{
        "provider": "qq", "provider_id": "c1", "title": "快乐崇拜",
        "artists": ["张韶涵"], "album": "欧若拉 (2004 再版)", "duration": 240.0,
    }]
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates",
        lambda **kw: CandidateJudgement(
            best="qq:c1", confidence=0.93, relation="same_recording",
            reason="曲名与艺人一致",
        ),
    )
    rows = _lyrics_source_rows(snap, candidates, cfg)
    by_key = {r["key"]: r for r in rows}
    assert "ALBUM" in by_key
    assert by_key["ALBUM"]["suggested"] == ["欧若拉 (2004 再版)"]


def test_歌词来源核验不通过只补空(tmp_path: Path, monkeypatch):
    """模型说不是同一首 → 只保留「纯补空」的行，冲突不碰。"""
    from lyra.tagscrape import _lyrics_source_rows

    cfg = _cfg(tmp_path)
    snap = _snap(tmp_path)
    candidates = [{
        "provider": "qq", "provider_id": "c1", "title": "快乐崇拜",
        "artists": ["张韶涵"], "album": "别的专辑", "duration": 240.0,
    }]
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates",
        lambda **kw: CandidateJudgement(
            best=None, confidence=0.2, relation="different", reason="不是同一首",
        ),
    )
    rows = _lyrics_source_rows(snap, candidates, cfg)
    # 本地已有 ALBUM=欧若拉，与「别的专辑」冲突 → 冲突行不生成
    assert all(r["action"] != "conflict" for r in rows)


def test_没有模型时歌词来源只补空(tmp_path: Path, monkeypatch):
    """模型不可用时不能改写已有值，只补空。"""
    from lyra.tagscrape import _lyrics_source_rows

    cfg = _cfg(tmp_path)
    cfg.ai.api_key = ""
    snap = _snap(tmp_path)
    candidates = [{
        "provider": "qq", "provider_id": "c1", "title": "快乐崇拜",
        "artists": ["张韶涵"], "album": "别的专辑", "duration": 240.0,
    }]
    calls: list = []
    monkeypatch.setattr(
        "lyra.tagscrape.review_candidates", lambda **kw: calls.append(kw) or None
    )
    rows = _lyrics_source_rows(snap, candidates, cfg)
    assert not calls
    assert all(r["action"] != "conflict" for r in rows)
