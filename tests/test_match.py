"""候选匹配与决策的测试。

为什么测这些：

匹配层决定「把哪首歌的歌词写进哪个文件」。它错了不会报错，只会**悄悄写错歌词**，
而这恰恰是用户最初的痛点。下面每条断言都对应一个实际确认过的判断：

- **时长闸门是硬约束。** 超出容差的候选直接否决，不是降权。
- **版本必须对称核验。** 现场版、重生版、粤语版的时长可能与录音室版完全相同；
  靠时长挑"最近的"会挑错。曲库里 `A.I.N.Y.` 就是反例：重生版差 0.20s，
  正确的原版差 0.80s。
- **时长 ≤1s 只用于消除「候选难分高下」这类假歧义。** 它**不**绕过版本校验，
  也**不**绕过置信度下限——否则会把"根本没匹配上"当成"候选接近"放行。
- **不确定就不写。** 低置信、版本冲突、时长超差一律进复核，绝不挑第一条。
"""

from __future__ import annotations

from types import SimpleNamespace

from lyra.config import MatchingPolicy, PolicyConfig
from lyra.match import Decision, build_context, decide, score_candidates

POLICY = PolicyConfig().matching


def cand(
    title: str,
    artist: str,
    duration: float | None,
    *,
    album: str = "",
    provider: str = "lrclib",
    provider_id: str = "x",
) -> SimpleNamespace:
    """构造一个鸭子类型的候选（与 providers.Candidate 字段一致）。"""
    return SimpleNamespace(
        provider=provider, provider_id=provider_id, title=title,
        artists=(artist,), album=album, duration=duration,
    )


def run(ctx, candidates, policy: MatchingPolicy = POLICY):
    return decide(ctx, score_candidates(ctx, candidates, policy), policy)


# ---------------------------------------------------------------------------
# 时长闸门
# ---------------------------------------------------------------------------


def test_时长超出容差直接否决():
    ctx = build_context({"TITLE": ["A.I.N.Y."], "ARTIST": ["G.E.M. 鄧紫棋"]}, 227.0)
    result = run(ctx, [cand("A.I.N.Y.", "G.E.M.邓紫棋", 280.0)])
    assert result.decision is Decision.NO_MATCH
    assert "硬约束" in result.reason


def test_时长差恰好等于容差时通过():
    """边界：容差是闭区间，正好等于阈值不应被否决。"""
    ctx = build_context({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0)
    result = run(ctx, [cand("泡沫", "G.E.M.邓紫棋", 260.0)])
    assert result.decision is Decision.ACCEPT


# ---------------------------------------------------------------------------
# 版本核验（关键：时长相同不能区分版本）
# ---------------------------------------------------------------------------


def test_本地现场版对候选录音室版必须复核():
    """实际：两者的时长**完全相同**，靠时长无法区分，必须靠版本标记。

    若这里放行，就会把录音室歌词写进现场版文件。
    """
    ctx = build_context({"TITLE": ["喜欢你 (Live)"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 262.0)
    result = run(ctx, [cand("喜欢你", "G.E.M.邓紫棋", 262.0)])
    assert result.decision is Decision.NEEDS_REVIEW
    assert "版本" in result.reason


def test_本地无标记对候选现场版必须复核():
    """反向同理：候选标了 Live 而本地没有，也不能算确认。"""
    ctx = build_context({"TITLE": ["天下"], "ARTIST": ["张杰"]}, 248.0)
    result = run(ctx, [cand("天下 (Live)", "张杰", 248.0)])
    assert result.decision is Decision.NEEDS_REVIEW


def test_版本标记一致时接受():
    ctx = build_context({"TITLE": ["喜欢你 (Live)"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 262.0)
    result = run(ctx, [cand("喜欢你 (Live)", "G.E.M.邓紫棋", 262.0)])
    assert result.decision is Decision.ACCEPT


def test_必须选原版而不是时长更近的重生版():
    """真实反例：重生版 Δt=0.20s 比正确的原版 Δt=0.80s **更近**。

    如果规则是「挑时长最近的」，这里会挑中重生版——歌词就错了。
    版本闸门必须先否决版本不一致的那个。
    """
    ctx = build_context({"TITLE": ["A.I.N.Y."], "ARTIST": ["G.E.M. 鄧紫棋"]}, 226.8)
    result = run(
        ctx,
        [
            cand("A.I.N.Y. 爱你 (G.E.M.重生版)", "G.E.M.邓紫棋", 227.0, provider_id="rerecord"),
            cand("A.I.N.Y.", "G.E.M.邓紫棋", 226.0, provider_id="orig"),
        ],
    )
    assert result.decision is Decision.ACCEPT
    assert result.best is not None and result.best.provider_id == "orig"
    assert abs((result.best.duration_delta or 0) - 0.8) < 0.01


def test_专辑名里的版本标记也算证据():
    """实际 `漂向北方` 的版本标记只出现在专辑名 `漂向北方 (live)` 里。"""
    ctx = build_context(
        {
            "TITLE": ["漂向北方"],
            "ARTIST": ["黄明志 & G.E.M. 鄧紫棋"],
            "ALBUM": ["漂向北方 (live)"],
        },
        265.0,
    )
    assert "live" in ctx.version_tokens
    result = run(ctx, [cand("飘向北方 (Live)", "G.E.M.邓紫棋", 265.0)])
    assert result.decision is Decision.ACCEPT


# ---------------------------------------------------------------------------
# 自动接受：时长 ≤ 1s 时消除假歧义
# ---------------------------------------------------------------------------


def test_时长吻合时同分候选自动接受():
    """用户策略：时长差小就不必为「候选难分高下」送复核。

    典型场景是 LRCLIB 上同一首歌有多条元数据完全相同的记录——
    它们指向同一录音，判为歧义纯属浪费人工。
    """
    ctx = build_context(
        {"TITLE": ["Still Breathing"], "ARTIST": ["Faouzia, G.E.M. 鄧紫棋 & ILLENIUM"]},
        219.3,
    )
    result = run(
        ctx,
        [
            cand("Still Breathing", "Faouzia/G.E.M.邓紫棋/ILLENIUM", 219.61, provider_id="a"),
            cand("Still Breathing", "Faouzia/G.E.M.邓紫棋/ILLENIUM", 219.61, provider_id="b"),
        ],
    )
    assert result.decision is Decision.ACCEPT
    assert "自动采用" in result.reason


def test_时长差大且同分才复核():
    """放松之后仍有一条底线：时长差超出「放松档」，同分候选仍须人工确认。

    这是硬闸门(5s)与放松档(3s)之间的窄带——真正难分高下时不该硬猜。
    """
    ctx = build_context({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0)
    result = run(
        ctx,
        [
            cand("泡沫", "G.E.M.邓紫棋", 262.0, provider_id="a"),
            cand("泡沫", "G.E.M.邓紫棋", 262.0, provider_id="b"),
        ],
    )
    assert result.decision is Decision.NEEDS_REVIEW


def test_时长很准时低分也自动接受():
    """默认：别太保守。时长差 ≤1.5s 是非常强的证据，
    此时分数只要达到较低的门槛就该写入，而不是推给人工。
    """
    ctx = build_context({"TITLE": ["突然之间"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 202.7)
    result = run(ctx, [cand("突然之间", "G.E.M.邓紫棋", 203.0)])
    assert result.decision is Decision.ACCEPT


def test_分数极低时仍然复核():
    """放松有下限：连低分数门槛都够不上，说明根本没匹配上。"""
    ctx = build_context({"TITLE": ["完全不相干的标题"], "ARTIST": ["某歌手"]}, 200.0)
    result = run(ctx, [cand("另一个完全不同的歌", "别的歌手", 200.0)])
    assert result.decision is Decision.NEEDS_REVIEW


def test_自动接受不能绕过版本校验():
    """时长完全相同也不能救版本冲突——这是最容易写错歌词的路径。"""
    ctx = build_context({"TITLE": ["喜欢你 (Live)"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 262.0)
    result = run(ctx, [cand("喜欢你", "G.E.M.邓紫棋", 262.0)])
    assert result.decision is Decision.NEEDS_REVIEW
    assert "版本" in result.reason


def test_有版本一致的备选时不打扰人工():
    """最优候选版本冲突，但另一个候选版本一致 → 用那个，不必送复核。

    这样既不会写错版本，也不会把「其实有正确答案」的情况推给人工。
    """
    ctx = build_context({"TITLE": ["突然之间"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 202.7)
    result = run(
        ctx,
        [
            cand("突然之间 (Live)", "G.E.M.邓紫棋", 202.7, provider_id="live"),
            cand("突然之间", "G.E.M.邓紫棋", 202.8, provider_id="studio"),
        ],
    )
    assert result.decision is Decision.ACCEPT
    assert result.best is not None and result.best.provider_id == "studio"


def test_自动接受阈值可配置():
    """把所有放松档归零即恢复「同分必复核」的保守行为。"""
    strict = MatchingPolicy(
        auto_accept_within_seconds=0.0,
        auto_accept_relaxed_seconds=0.0,
        auto_accept_min_confidence=1.0,
        auto_accept_relaxed_confidence=1.0,
        auto_accept_wide_confidence=1.0,
    )
    ctx = build_context({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0)
    result = run(
        ctx,
        [
            cand("泡沫", "G.E.M.邓紫棋", 258.2, provider_id="a"),
            cand("泡沫", "G.E.M.邓紫棋", 258.2, provider_id="b"),
        ],
        strict,
    )
    assert result.decision is Decision.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_繁简与空格差异不影响艺人匹配():
    """标签是繁体、QQ 返回简体，且空格写法不同——不归一化会恒为 0 分。"""
    ctx = build_context({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0)
    scores = score_candidates(ctx, [cand("泡沫", "G.E.M.邓紫棋", 258.0)], POLICY)
    assert scores[0].artist_score >= 0.99


def test_含和号的歌手串被正确拆分():
    """实际含 `&` 的整串查询会让 QQ 搜索返回 0 条，必须先拆开。"""
    ctx = build_context({"TITLE": ["漂向北方"], "ARTIST": ["黄明志 & G.E.M. 鄧紫棋"]}, 265.0)
    assert ctx.artists == ("黄明志", "G.E.M. 鄧紫棋")


def test_优先使用显式多值歌手字段():
    """Picard 为合作曲目写 ARTISTS 多项，它比拆分 ARTIST 合并串更准确。"""
    ctx = build_context(
        {
            "TITLE": ["Sacrifice"],
            "ARTIST": ["Riot Games Music Team & G.E.M. 鄧紫棋"],
            "ARTISTS": ["Riot Games Music Team", "G.E.M. 鄧紫棋"],
        },
        200.0,
    )
    assert ctx.artists == ("Riot Games Music Team", "G.E.M. 鄧紫棋")


def test_无候选时不声称已找到():
    ctx = build_context({"TITLE": ["泡沫"], "ARTIST": ["G.E.M. 鄧紫棋"]}, 258.0)
    result = run(ctx, [])
    assert result.decision is Decision.NO_MATCH
    assert result.best is None


# ---------------------------------------------------------------------------
# 候选未提供艺人信息时，艺人闸门应「不适用」而非失败
# ---------------------------------------------------------------------------


def _ctx():
    from lyra.match import MatchContext

    return MatchContext(title="春天里", artists=("G.E.M. 鄧紫棋", "方大同"), album="",
                        duration=283.1)


def _score(*, has_artists: bool, artist_score: float):
    """直接构造打分结果，隔离闸门行为，不依赖上游的候选解析。"""
    from lyra.match import CandidateScore

    return CandidateScore(
        provider="qq", provider_id="x", score=0.80, duration_delta=0.1,
        title_score=0.95, artist_score=artist_score, album_score=0.0,
        version_ok=True, has_artists=has_artists,
    )


def test_候选无艺人时闸门不适用():
    """**假阴性**：来源没给艺人字段，被当成「艺人不对」。

    实际 `07 春天里` 的候选歌词完全正确（标题里就写着
    `G.E.M. 邓紫棋/方大同`），却因艺人字段为空被永久拦在自动接受之外。

    「来源没给这个字段」与「这个字段是错的」是两件事。
    真正的保护来自标题闸门——那里比的是歌名，无法造假。
    """
    from lyra.match import MatchingPolicy, evaluate_gates

    gates = {
        g.key: g for g in evaluate_gates(
            _ctx(), _score(has_artists=False, artist_score=0.0), MatchingPolicy()
        )
    }
    artist = gates["artist"]
    assert artist.passed is True, f"不该判失败，实际 detail：{artist.detail}"
    assert "不适用" in artist.detail


def test_候选提供了艺人但仍不匹配时照常失败():
    """放宽的是「没有数据」，不是「数据不对」。有数据就必须真匹配。"""
    from lyra.match import MatchingPolicy, evaluate_gates

    gates = {
        g.key: g for g in evaluate_gates(
            _ctx(), _score(has_artists=True, artist_score=0.0), MatchingPolicy()
        )
    }
    assert gates["artist"].passed is False, "提供了艺人却不匹配，仍应失败"


def test_候选艺人匹配时正常通过():
    from lyra.match import MatchingPolicy, evaluate_gates

    gates = {
        g.key: g for g in evaluate_gates(
            _ctx(), _score(has_artists=True, artist_score=0.98), MatchingPolicy()
        )
    }
    assert gates["artist"].passed is True


# ---------------------------------------------------------------------------
# 艺人串展开：去括号别名 + 拆分组合串
# ---------------------------------------------------------------------------


def test_艺人带括号别名仍能匹配():
    """实际 `G.E.M. 邓紫棋 (Gem Tang)` 相似度只有 0.489，**差 0.011 被拒**。

    本地是 `G.E.M. 鄧紫棋`，繁简归一本来是 1.000——
    多出来的英文名括注把它拉到了门槛之下。
    """
    from lyra.match import _artist_score

    ctx = ("G.E.M. 鄧紫棋", "方大同")
    assert _artist_score(ctx, ("G.E.M. 邓紫棋 (Gem Tang)",)) >= 0.5
    assert _artist_score(ctx, ("G.E.M. 邓紫棋 (Gem Tang)", "方大同 (Khalil Fong)")) >= 0.5


def test_组合艺人串会被拆开():
    """来源常把合作曲目给成一个整串，实际相似度只有 0.304。"""
    from lyra.match import _artist_score

    ctx = ("G.E.M. 鄧紫棋", "方大同")
    for form in (
        ("G.E.M. 邓紫棋(Gem Tang)/方大同(Khalil Fong)",),
        ("G.E.M. 邓紫棋 & 方大同",),
        ("G.E.M. 邓紫棋 feat. 方大同",),
        ("G.E.M. 邓紫棋、方大同",),
    ):
        assert _artist_score(ctx, form) >= 0.5, f"{form} 应能匹配"


def test_拆分别名后仍能拒绝真正不相关的艺人():
    """放宽的是**表述形式**，不是判断标准。"""
    from lyra.match import _artist_score

    ctx = ("G.E.M. 鄧紫棋", "方大同")
    assert _artist_score(ctx, ("宋茜",)) < 0.5
    assert _artist_score(ctx, ("屋顶着火",)) < 0.5


def test_纯括号的艺人名不会被抹成空():
    """去括号后为空时保留原串，不能把名字弄没。"""
    from lyra.match import _expand_artists

    assert _expand_artists(("(未知)",)) == ("(未知)",)
    assert _expand_artists(("", "  ")) == ()


# ---------------------------------------------------------------------------
# 歌手串拆分：句点残留（实际把 `. G.E.M. 鄧紫棋` 当成一个歌手）
# ---------------------------------------------------------------------------


def test_feat_后面的句点不会被留下():
    """`阿信 feat. G.E.M. 鄧紫棋` 曾拆成 `('阿信', '. G.E.M. 鄧紫棋')`。

    成因：正则写成 `\\bfeat\\.?\\b`，而 `feat. ` 里句点后紧跟空格，
    `\\.` 与 `\\b` 无法同时成立，回溯后只吃掉 `feat`，`". "` 留给下一个歌手。
    后果有两层：查询上游时用了一个不存在的歌手名，打分时艺人相似度也算错。
    """
    from lyra.match import split_artists

    assert split_artists("阿信 feat. G.E.M. 鄧紫棋") == ("阿信", "G.E.M. 鄧紫棋")
    assert split_artists("A ft. B") == ("A", "B")
    assert split_artists("A vs. B") == ("A", "B")
    assert split_artists("A with B") == ("A", "B")
    assert split_artists("A x B") == ("A", "B")


def test_歌手名不会被标点污染():
    """拆分后的碎片必须剥掉首尾标点——这是第二道保险。"""
    from lyra.match import split_artists

    for value in ("阿信 feat. G.E.M. 鄧紫棋", ". G.E.M. 鄧紫棋", "- G.E.M. 鄧紫棋"):
        for name in split_artists(value):
            assert name == name.strip(" .,-_·・,，、&/|"), f"{name!r} 仍带标点"  # noqa: B005


def test_不误伤本来就含空格或字母的名字():
    """放宽分隔符不能把正常名字切坏。"""
    from lyra.match import split_artists

    assert split_artists("Malcolm X") == ("Malcolm X",)
    assert split_artists("G.E.M. 鄧紫棋") == ("G.E.M. 鄧紫棋",)
    assert split_artists("S.H.E.") == ("S.H.E.",)


# ---------------------------------------------------------------------------
# 艺人相似度：署名串很长的本地标签
# ---------------------------------------------------------------------------


def test_本地署名串很长时歌手仍能匹配():
    """实际 `我们的梦.flac` 的 ARTIST 里排了 20 位歌手，`G.E.M.邓紫棋` 排第 8。

    按本地侧取平均时，候选正是 `G.E.M.邓紫棋` 本人也只有 1/20 ≈ 0.05 分。
    地板分原本只看 `ctx_artists[0]`（谭咏麟），于是**永远不触发**。
    """
    from lyra.match import _artist_score

    long_credit = (
        "谭咏麟", "钟镇涛", "陈慧娴", "许志安", "梁汉文", "陈晓东", "李幸倪",
        "G.E.M.邓紫棋", "AGA", "刘浩龙", "符致逸", "麦家瑜", "周殷廷",
        "赵浚承", "罗钧满", "Robynn", "Kendy", "欧阳靖", "Gold Mountain",
        "Artem Ansheles",
    )
    assert _artist_score(long_credit, ("G.E.M.邓紫棋",)) >= 0.5
    # 地板分不能变成「有人沾边就放行」：不在这条署名串里的歌手仍要被拒
    assert _artist_score(long_credit, ("林火火",)) < 0.5


def test_地板分要求高度吻合而不是沾边():
    """地板分的触发条件是 ≥0.9，不是「有一点点像」。

    本地是多人署名、候选只与其中一位中等相似时，平均会把分数摊薄，
    这一类候选必须被拒——否则「沾边就放行」会让艺人闸门失去意义。
    """
    from lyra.match import _artist_score

    # `谭咏麟乐队` 与 `谭咏麟` 相似度 0.6（<0.9），另一位完全不沾边
    assert _artist_score(("谭咏麟", "钟镇涛"), ("谭咏麟乐队",)) < 0.5
    # 高度吻合时地板分生效（第二位不吻合也不影响）
    assert _artist_score(("谭咏麟", "钟镇涛"), ("谭咏麟",)) >= 0.9


def test_合作曲目署名不对称时仍能匹配():
    """本地 `阿信 feat. G.E.M. 鄧紫棋`，QQ `五月天 / G.E.M.邓紫棋`。"""
    from lyra.match import _artist_score

    assert _artist_score(("阿信", "G.E.M. 鄧紫棋"), ("五月天", "G.E.M.邓紫棋")) >= 0.5


# ---------------------------------------------------------------------------
# 决策：跳过「冒名」的最优候选
# ---------------------------------------------------------------------------


def _cand(provider_id, *, score, artist_score, title_score=0.97, delta=0.85,
          version_ok=True, provider="qq"):
    from lyra.match import CandidateScore

    return CandidateScore(
        provider=provider, provider_id=provider_id, score=score,
        duration_delta=delta, title_score=title_score, artist_score=artist_score,
        album_score=0.0, version_ok=version_ok,
        has_artists=True, provider_rank=0,
    )


def test_跳过冒名候选改用艺人证据成立的备选():
    """标题一字不差但歌手是别人的候选，不该因为分数高就顶掉证据齐全的那条。

    实际动机：QQ 上存在**上传者冒名**的记录——标题与本地完全相同、时长也
    只差零点几秒，但歌手字段填的是上传者。它按标题相似度必然排第一。
    旧逻辑会拿它去走后面的分档判断，而 `evaluate_gates` 同时把它的
    「艺人相似度」标成未通过：判据与结论互相矛盾。

    这里断言的是「改用证据齐全的那条」，不是「放宽艺人闸门」。
    """
    from lyra.match import Decision, MatchingPolicy, decide

    ctx = _ctx()
    impersonator = _cand("impersonator", score=0.80, artist_score=0.0, title_score=1.0)
    real = _cand("real", score=0.78, artist_score=0.9, title_score=0.97)
    assert impersonator.score > real.score, "前提：冒名者分数更高，才会排第一"
    decision = decide(ctx, [impersonator, real], MatchingPolicy())

    assert decision.decision is Decision.ACCEPT
    assert decision.best is not None
    assert decision.best.provider_id == "real", (
        f"应采用艺人证据成立的那条，实际用了 {decision.best.provider_id}"
    )
    assert any("跳过艺人证据不成立" in d for d in decision.best.details)


def test_内容证据不足时绝不自动接受():
    """即使分数高、时长准，内容证据不齐也不能写。

    旧代码的收尾分支只看分数与时长差，于是艺人相似度 0.00 的候选照样会被
    ACCEPT，而同一个候选在 `evaluate_gates` 里显示「艺人相似度：未通过」。
    这里把「判据」与「结论」钉成一致。
    """
    from lyra.match import Decision, MatchingPolicy, decide

    ctx = _ctx()
    mismatch = _cand("mismatch", score=0.86, artist_score=0.0, title_score=0.99)
    assert decide(ctx, [mismatch], MatchingPolicy()).decision is Decision.NEEDS_REVIEW


def test_没有艺人证据成立的备选时仍然转复核():
    """放宽的只是「后面还有对得上的」，不是「艺人不对也能写」。"""
    from lyra.match import Decision, MatchingPolicy, decide

    ctx = _ctx()
    only = _cand("金布袋", score=0.734, artist_score=0.0, title_score=1.0)
    assert decide(ctx, [only], MatchingPolicy()).decision is Decision.NEEDS_REVIEW


def test_备选必须同时满足标题与版本证据():
    """备选只有时长对得上是不够的——标题或版本不合格就不能顶替。"""
    from lyra.match import Decision, MatchingPolicy, decide

    ctx = _ctx()
    impersonator = _cand("impersonator", score=0.80, artist_score=0.0, title_score=1.0)
    bad_title = _cand("bad-title", score=0.79, artist_score=0.9, title_score=0.40)
    bad_version = _cand("bad-version", score=0.78, artist_score=0.9,
                        title_score=0.97, version_ok=False)
    decision = decide(ctx, [impersonator, bad_title, bad_version], MatchingPolicy())
    assert decision.decision is Decision.NEEDS_REVIEW
    assert decision.best.provider_id == "impersonator"
