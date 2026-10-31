"""扫描互斥、done 提案静默跳过、AI 选中候选的闸门内切换。

三个问题都来自 2.2.8 上线后的真实生产日志：

1. **并发扫描**：监控的 4 小时周期扫描与人工 CLI 扫描同时跑，一方
   采到的提案 id 在另一方写入前状态已变，写入阶段报「这条提案不是
   自动项」；两份全库查询还打满 MusicBrainz 限流配额。
2. **done 提案报写入失败**：`upsert_tag_proposal` 对人工已定案
   （done）的行原样保留并返回 id，扫描把它当作 auto 采集进写入
   列表，`apply_proposal` 拒绝——那是预期跳过，不是故障。
3. **AI 判定被浪费**：模型确认「同一录音、置信 0.93」但选中的是
   另一条候选（程序按分数暂存了同曲的另一版本）。旧设计直接保留
   分歧给人，生产实测约 30% 的判定因此空转。新设计：若模型选中的
   候选**自己也过得了全部硬闸门**（时长/版本/置信/标题/艺人），
   就取它的歌词替换——闸门是硬约束，模型只在过闸候选里挑。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.tagscrape import _write_auto

# ---------------------------------------------------------------------------
# 1) 扫描互斥
# ---------------------------------------------------------------------------


def test_已有扫描时第二轮直接跳过(tmp_path: Path):
    from lyra.tagscrape import scan_tags

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(tmp_path / "lib"))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    assert store.acquire_write_lock("tags-scan", "other", ttl_seconds=600)

    class _DeadProvider:
        name = "mb"
        available = True

        def health_check(self) -> bool:  # pragma: no cover - 不应被调用
            raise AssertionError("互斥生效时不应触碰来源")

    summary = scan_tags(
        cfg, cfg.roots[0], store, _DeadProvider(),  # type: ignore[arg-type]
    )
    assert any("已有标签扫描在进行" in e for e in summary.errors)
    store.release_write_lock("tags-scan", "other")
    store.close()


# ---------------------------------------------------------------------------
# 2) done/ignored 提案在写入阶段静默跳过
# ---------------------------------------------------------------------------


def test_done提案静默跳过不报错(tmp_path: Path):
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(tmp_path / "lib"))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(name="lib", role="library",
                                path=str(tmp_path / "lib"), enabled=True,
                                read_only=False)
    loc = store.upsert_location(root_id=root_id, rel_path="a.flac",
                                size=1, mtime=1.0)
    pid = store.upsert_tag_proposal(
        location_id=loc, state="done", source="search", confidence=0.9,
        title="歌", artists="人",
        payload={"rows": [], "candidate": {}, "match": {}},
    )
    from lyra.tagscrape import TagScanSummary

    summary = TagScanSummary()
    _write_auto(store, cfg, cfg.roots[0], [pid], summary, lambda _m: None)
    assert summary.errors == [], "done 是预期跳过，不应报写入失败"
    assert summary.written == 0
    store.close()


# ---------------------------------------------------------------------------
# 3) AI 选中另一条候选：闸门内换用
# ---------------------------------------------------------------------------


class _TwoCandidateProvider:
    """两条同分候选（时长差 0.05s < min_score_margin）：程序判「需复核」
    并按分数暂存 A 的词；模型读候选元数据后确认 B 才是同一录音。

    生产真实场景（netease:254804 vs netease:255040）：模型选中的候选
    不是程序按分数排的第一名——换用模型的选择而不是把分歧留给人。
    """

    name = "qq"

    def __init__(self, lyrics_a: str, lyrics_b: str) -> None:
        self._lyrics = {"A": lyrics_a, "B": lyrics_b}

    def search(self, _query):  # noqa: ANN001, ANN202
        from lyra.providers.base import Candidate

        return [
            # A 与 B 同分（时长差 0.05s < min_score_margin 0.15）→
            # 程序判「需复核」（歧义），按分数暂存 A 的词。
            Candidate(provider="qq", provider_id="A", title="夜曲",
                      artists=("周杰伦",), duration=3.0),
            # B：模型读元数据后认定的同一录音（时长差 0.05s，过全部硬闸门）
            Candidate(provider="qq", provider_id="B", title="夜曲",
                      artists=("周杰伦",), duration=3.05),
        ]

    def lyrics(self, candidate):  # noqa: ANN001, ANN202
        text = self._lyrics.get(candidate.provider_id)
        if not text:
            return None
        from lyra.providers.base import RawLyrics

        return RawLyrics(
            provider="qq", provider_id=candidate.provider_id, text=text,
            synced=True,
        )

    def close(self) -> None:
        pass


def test_AI选中的候选过闸时换用(tmp_path: Path, monkeypatch):
    cfg = Config(
        roots=[RootConfig(name="main", path=str(tmp_path / "lib"))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.write_enabled = True
    cfg.ai.enabled = True
    cfg.ai.pre_review = True
    cfg.ai.auto_accept = True
    cfg.ai.pre_review_confidence = 0.85
    lib = tmp_path / "lib"
    # 本地标题部分相似（标题分 ~0.57：低于 content 闸门 0.85、高于
    # 替换底线 0.5），程序只能判「需复核」；模型读候选元数据后确认
    # B 是同一录音——生产实测的真实形态（数值分被伪影拖低）。
    make_flac(lib / "夜曲.flac", entries=(("TITLE", "夜曲钢琴版"), ("ARTIST", "周杰伦")))
    store = open_store(cfg.storage.resolved_data_dir)

    from lyra.ai import CandidateJudgement
    from lyra.pipeline import run_fetch

    judgement = CandidateJudgement(
        best="qq:B", confidence=0.93, relation="same_recording",
        reason="标题艺人一致、时长几乎相同，来源标注为同一录音",
    )
    monkeypatch.setattr(
        "lyra.pipeline._ai_judge_candidates",
        lambda *args, **kwargs: judgement,
    )

    lyrics_a = "[00:01.00]现场版的正文\n[00:02.00]第二行\n"
    lyrics_b = "[00:01.00]模型选中的那条的正文\n[00:02.00]第二行\n"
    summary = run_fetch(
        store, cfg, cfg.roots[0], [_TwoCandidateProvider(lyrics_a, lyrics_b)],
    )
    assert summary.ai_accepted == 1, f"AI 换用应被采纳：{summary.errors}"
    rows = store.conn.execute(
        "SELECT provider, provider_id, state, origin, content FROM staged_lyrics"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "B", "应暂存模型选中的候选"
    assert rows[0]["origin"] == "ai"
    assert rows[0]["state"] == "staged"
    assert "模型选中的那条" in rows[0]["content"]
    store.close()


def test_AI选中的候选闸门不过时保持原判(tmp_path: Path, monkeypatch):
    """模型选的候选时长差过大（超闸门）：分歧留给人，不硬写。"""
    cfg = Config(
        roots=[RootConfig(name="main", path=str(tmp_path / "lib"))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.write_enabled = True
    cfg.ai.enabled = True
    cfg.ai.pre_review = True
    cfg.ai.auto_accept = True
    cfg.ai.pre_review_confidence = 0.85
    lib = tmp_path / "lib"
    make_flac(lib / "夜曲.flac", entries=(("TITLE", "夜曲钢琴版"), ("ARTIST", "周杰伦")))
    store = open_store(cfg.storage.resolved_data_dir)

    from lyra.ai import CandidateJudgement
    from lyra.pipeline import run_fetch
    from lyra.providers.base import Candidate

    judgement = CandidateJudgement(
        best="qq:C", confidence=0.95, relation="same_recording",
        reason="模型认为是同一录音",
    )
    monkeypatch.setattr(
        "lyra.pipeline._ai_judge_candidates",
        lambda *args, **kwargs: judgement,
    )

    class _FarCandidateProvider:
        name = "qq"

        def search(self, _query):
            return [
                Candidate(provider="qq", provider_id="A", title="夜曲",
                          artists=("周杰伦",), duration=3.0),
                # C：模型选中的候选，时长差 27s —— 超硬外闸（5s），不换用
                Candidate(provider="qq", provider_id="C", title="夜曲",
                          artists=("周杰伦",), duration=30.0),
            ]

        def lyrics(self, c):
            if c.provider_id != "A":
                return None
            from lyra.providers.base import RawLyrics

            return RawLyrics(provider="qq", provider_id="A",
                             text="[00:01.00]A的词\n", synced=True)

        def close(self) -> None:
            pass

    summary = run_fetch(
        store, cfg, cfg.roots[0], [_FarCandidateProvider()],
    )
    assert summary.ai_accepted == 0, "闸门不过不能换用"
    assert summary.ai_suggested >= 1, "分歧进入待办"
    store.close()


def test_替换候选的安全底线():
    """分数闸门不适用于模型仲裁（数值分被脏文件名拖低是常态），
    但「毫不相关」的硬底线必须守住：rejected 不复活、时长差在硬外闸内、
    标题相似度 ≥0.5。"""
    from lyra.config import MatchingPolicy
    from lyra.match import CandidateScore
    from lyra.pipeline import _alt_candidate_admissible

    policy = MatchingPolicy()
    good = CandidateScore(
        provider="qq", provider_id="a", score=0.55, duration_delta=0.3,
        title_score=0.9, artist_score=0.9, album_score=0.8, version_ok=False,
    )
    assert _alt_candidate_admissible(good, policy), "低分但模型仲裁的候选应可用"
    far = CandidateScore(
        provider="qq", provider_id="a", score=0.95, duration_delta=30.0,
        title_score=0.98, artist_score=0.95, album_score=0.9, version_ok=True,
    )
    assert not _alt_candidate_admissible(far, policy), "时长差 30s 是另一首歌"
    rejected = CandidateScore(
        provider="qq", provider_id="a", score=0.9, duration_delta=0.1,
        title_score=0.9, artist_score=0.9, album_score=0.9, version_ok=True,
        rejected=True,
    )
    assert not _alt_candidate_admissible(rejected, policy), "被否决的不复活"
    off_title = CandidateScore(
        provider="qq", provider_id="a", score=0.9, duration_delta=0.1,
        title_score=0.3, artist_score=0.9, album_score=0.9, version_ok=True,
    )
    assert not _alt_candidate_admissible(off_title, policy), "标题 0.3 是另一首歌"
