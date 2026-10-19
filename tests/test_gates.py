"""逐闸门判定与采用建议的测试。

为什么必须固定：复核界面给出的建议会直接引导人点「接受」。
如果建议把「另一首歌」误判成「可以接受」，用户就会写下错误歌词——
而这个案例真实发生过（见下）。

真实案例（曲库 `06 G.E.M. 鄧紫棋 - 如果没有你.flac`）：
    QQ 返回的候选是 **泡沫**，同艺人、时长只差 0.85s，总分 0.573。
    旧文案说「置信度 0.573 低于门槛 0.75」——照着这句话调低置信度门槛，
    就会把泡沫的歌词写进《如果没有你》。真正的事实是**标题相似度 0.00**。
"""

from __future__ import annotations

from types import SimpleNamespace

from lyra.config import PolicyConfig
from lyra.match import build_context, decide, score_candidates

POLICY = PolicyConfig().matching


def cand(title, artist, duration, *, album="", provider_id="x"):
    return SimpleNamespace(provider="qq", provider_id=provider_id, title=title,
                           artists=(artist,), album=album, duration=duration)


def run(tags, duration, candidates):
    ctx = build_context(tags, duration)
    return ctx, decide(ctx, score_candidates(ctx, candidates, POLICY), POLICY)


def test_另一首歌会被明确指认为标题不匹配():
    """真实案例：同艺人、Δt=0.85s，但标题是另一首歌。"""
    _ctx, result = run(
        {"TITLE": ["如果没有你"], "ARTIST": ["G.E.M. 鄧紫棋"],
         "ALBUM": ["我是歌手第二季 第7期"]},
        257.15,
        [cand("泡沫", "G.E.M.邓紫棋", 258.0)],
    )
    assert result.decision.value == "needs_review"
    title_check = next(c for c in result.checks if c.key == "title")
    assert not title_check.passed
    assert "0.000" in title_check.detail
    # 关键：必须建议「不要采用」，而不是让人去放宽阈值
    assert result.recommendation is not None
    assert result.recommendation.verdict == "reject"
    assert "不是同一首歌" in result.recommendation.text


def test_时长与艺人吻合但标题差一点时建议采用():
    """繁简/括号/空格差异导致的轻微标题不符，应当建议采用而不是拒绝。"""
    _ctx, result = run(
        {"TITLE": ["突然之间"], "ARTIST": ["G.E.M. 鄧紫棋"]},
        202.7,
        [cand("突然之间 (Live)", "G.E.M.邓紫棋", 202.8)],
    )
    assert result.recommendation is not None
    assert result.recommendation.verdict in ("adopt", "inspect")


def test_版本冲突必须给出人工判断而非采纳():
    ctx, result = run(
        {"TITLE": ["喜欢你 (Live)"], "ARTIST": ["G.E.M. 鄧紫棋"]},
        262.0,
        [cand("喜欢你", "G.E.M.邓紫棋", 262.0)],
    )
    assert result.decision.value == "needs_review"
    assert result.recommendation is not None
    assert result.recommendation.verdict == "inspect"
    version = next(c for c in result.checks if c.key == "version")
    assert not version.passed


def test_自动接受时也带完整的闸门记录():
    """自动接受的结果同样要有逐闸门记录，便于事后审计「凭什么自动写了」。"""
    _ctx, result = run(
        {"TITLE": ["突然之间"], "ARTIST": ["G.E.M. 鄧紫棋"]},
        202.7,
        [cand("突然之间", "G.E.M.邓紫棋", 202.8)],
    )
    assert result.decision.value == "accept"
    assert len(result.checks) == 5
    assert all(c.passed for c in result.checks)


def test_无候选时不产生建议():
    _ctx, result = run({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0, [])
    assert result.best is None
    assert result.recommendation is None
