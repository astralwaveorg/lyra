"""候选匹配与置信度判定。

这是「刮削准不准」的核心。设计要点：

1. **时长闸门是硬约束，也是主力判据。** 异体字或译名会让严格标题匹配
   失败，最终仍要靠「主艺人 + 时长 ±1s」命中。
2. **含 `&` 的多歌手串会让 QQ 搜索归零**，所以歌手必须先拆分；这里提供拆分，
   查询策略由 Provider 负责。
3. **版本必须核验。** 现场/重生版/伴奏/串烧的歌词与录音室版不同，
   时长接近也不能当作正确性证明。
4. **繁简必须归一化后比较。** 标签是繁体、QQ 返回简体，
   不归一化会导致艺人匹配恒为 0 分。
5. **不确定就不写。** 歧义（最优与次优接近）、证据不足、显式版本冲突
   一律进入复核，而不是挑第一条。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from enum import StrEnum

from .config import MatchingPolicy

__all__ = [
    "MatchContext",
    "CandidateScore",
    "MatchDecision",
    "Decision",
    "build_context",
    "split_artists",
    "normalize_text",
    "extract_version_tokens",
    "strip_bracketed",
    "score_candidate",
    "score_candidates",
    "decide",
]

try:  # pragma: no cover - 取决于环境
    from zhconv import convert as _zh_convert
except ImportError:  # 缺依赖时退化为不做繁简转换，而不是崩溃
    _zh_convert = None


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

_FULLWIDTH_PUNCT = str.maketrans({
    "（": "(", "）": ")", "【": "[", "】": "]", "「": "(", "」": ")",
    "，": ",", "、": ",", "：": ":", "；": ";", "！": "!", "？": "?",
    "～": "~", "－": "-", "—": "-", "–": "-", "·": " ", "・": " ",
})

# 参与比较时应忽略的装饰性括号内容之外的噪声
_NOISE = re.compile(r"[\"'`´’“”‘’]")

_BRACKETED = re.compile(r"[\(\[（【「]\s*([^\)\]）】」]*)\s*[\)\]）】」]")


def normalize_text(value: str, *, fold_traditional: bool = True) -> str:
    """比较用的规范形式。

    只用于**比较**：绝不写回文件，也不改变原始路径。
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).translate(_FULLWIDTH_PUNCT)
    text = _NOISE.sub("", text)
    if fold_traditional and _zh_convert is not None:
        text = _zh_convert(text, "zh-cn")
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 版本标记
# ---------------------------------------------------------------------------

#: 版本 → 同义词。命中任一即归入该版本标记。
_VERSION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "live": ("live", "现场", "現場", "演唱会", "演唱會", "音乐会", "音樂會", "unplugged live"),
    "remix": ("remix", "rmx", "混音", "重混", "remix版"),
    "instrumental": ("instrumental", "伴奏", "off vocal", "offvocal", "karaoke", "卡拉ok"),
    "acoustic": ("acoustic", "不插电", "不插電", "acoustic version"),
    "demo": ("demo", "小样", "小樣", "试听版"),
    "rerecorded": ("重生版", "重制", "重製", "remaster", "remastered", "re-recorded", "rerecorded", "新録"),
    "medley": ("串烧", "串燒", "medley", "组曲", "組曲", "mashup"),
    "cover": ("cover", "翻唱"),
    "chorus": ("合唱", "群星", "all stars"),
    "piano": ("piano", "钢琴", "鋼琴", "钢琴版"),
    "guitar": ("guitar", "吉他", "吉他版"),
    "ost": ("ost", "原声", "原聲", "主题曲", "主題曲", "片尾曲", "插曲"),
    # 语言版本是**显式标记**时才算证据；不能由字形推断语言
    "cantonese": ("粤", "粵", "粤语", "粵語", "cantonese"),
    "mandarin": ("国语", "國語", "普通话", "mandarin"),
}

# 由长到短匹配，避免 "live" 抢先命中 "unplugged live"
_VERSION_PATTERNS: list[tuple[str, re.Pattern[str]]] = sorted(
    (
        (label, re.compile("|".join(re.escape(s) for s in synonyms)))
        for label, synonyms in _VERSION_SYNONYMS.items()
    ),
    key=lambda item: -max(len(s) for s in _VERSION_SYNONYMS[item[0]]),
)


#: 不影响「录音本身」的标记。这些描述的是发行语境而非另一个录音版本
#: （同一录音既可以是「某剧主题曲」，也是专辑里的普通曲目），
#: 因此不参与版本一致性闸门，只作为证据展示。
NON_RECORDING_TOKENS: frozenset[str] = frozenset({"ost"})


def recording_versions(tokens: frozenset[str]) -> frozenset[str]:
    """只保留影响录音本身的版本标记，用于一致性闸门。"""
    return tokens - NON_RECORDING_TOKENS


def extract_version_tokens(title: str) -> frozenset[str]:
    """从标题（含括号内容）中提取版本标记。

    返回规范化的版本标签集合，例如 `"光年之外 (Live)"` → `{"live"}`。
    """
    if not title:
        return frozenset()
    text = unicodedata.normalize("NFKC", title).translate(_FULLWIDTH_PUNCT).casefold()
    if _zh_convert is not None:
        # 繁简归一后再匹配同义词表，避免粤/粵 各自维护
        text = _zh_convert(text, "zh-cn")
    found: set[str] = set()
    for label, pattern in _VERSION_PATTERNS:
        if pattern.search(text):
            found.add(label)
    return frozenset(found)


def strip_bracketed(title: str) -> str:
    """去掉括号内容，得到「基标题」。"""
    return _BRACKETED.sub(" ", title)


# ---------------------------------------------------------------------------
# 歌手拆分
# ---------------------------------------------------------------------------

#: 歌手分隔符。实际含 `&` 的整串会让 QQ 搜索返回 0 条，因此必须拆分。
#:
#: **带句点的写法（feat./ft./vs.）必须要求两侧都是空白，不能写成
#: `\bfeat\.?\b`。** 原因：`feat. ` 里句点后面跟着空格，`\.` 与 `\b`
#: 无法同时成立（`.` 与空格都是非单词字符），正则回溯后只吃掉 `feat`，
#: 把 `". "` 留给了下一个歌手。实际本地标签
#: `阿信 feat. G.E.M. 鄧紫棋` 被拆成 `('阿信', '. G.E.M. 鄧紫棋')`：
#: 这个假歌手名既被拿去查询上游（`G.E.M. 鄧紫棋` 反而查不到），
#: 又让艺人相似度算错。同一个坑在 `providers/lrclib.py` 里已经修过一次，
#: 这里是漏改的第二处。
#: 顺带把 `x`（合作记法 `A x B`）也改成必须两侧空白——旧写法会把
#: `Malcolm X` 这类名字截断成 `Malcolm`。
_ARTIST_SEP = re.compile(
    r"\s*(?:&|＆|/|／|、|,|，|;|；|\+|×)\s*"
    r"|\s+(?:with|feat\.?|ft\.?|vs\.?|x)\s+",
    re.IGNORECASE,
)

#: 拆分后残留在首尾的标点。上面的正则修好之后理论上不会再产生，
#: 但「歌手名被标点污染」这件事已经造成过两次真实误判（查询与打分各一次），
#: 所以这里再做一次兜底剥离。
#: 注意尾部**不含句点**：`G.E.M.`、`S.H.E.` 以句点结尾是正常写法，
#: 剥掉会把用来查询上游的名字改坏。
_ARTIST_LEAD = " \t.-_·・,，、&/|;；"
_ARTIST_TAIL = " \t-_·・,，、&/|;；"


def split_artists(value: str | None) -> tuple[str, ...]:
    """把歌手字段拆成多值。

    注意：真实艺人名里也可能含逗号（如 "Earth, Wind & Fire"）。
    这里宁可**过度拆分**——比较用的是集合相似度而非全等，
    多拆一个碎片不会造成误判，但漏拆会让查询直接归零。
    """
    if not value:
        return ()
    parts = [
        cleaned
        for cleaned in (
            # 必须 lstrip/rstrip 分开：`str.strip(chars)` 两端都剥，
            # 会把 `S.H.E.` 结尾的句点也吃掉（实际踩到）。
            p.lstrip(_ARTIST_LEAD).rstrip(_ARTIST_TAIL)
            for p in _ARTIST_SEP.split(value)
        )
        if cleaned
    ]
    # 去重但保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = normalize_text(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return tuple(out)


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchContext:
    """从本地文件标签构造的匹配上下文。"""

    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    version_tokens: frozenset[str] = frozenset()
    musicbrainz_trackid: str | None = None
    isrc: str | None = None

    @property
    def base_title(self) -> str:
        return strip_bracketed(self.title)

    @property
    def has_minimum_metadata(self) -> bool:
        """至少要有标题，否则不该发请求。"""
        return bool(normalize_text(self.title))


def build_context(
    tags: dict[str, list[str]],
    duration: float | None,
) -> MatchContext:
    """从 mutagen 读到的标签构造匹配上下文。

    `ALBUMARTIST` **不能**代替实际演唱者。
    """
    def first(key: str) -> str:
        values = tags.get(key) or []
        return values[0].strip() if values else ""

    title = first("TITLE")
    # 优先使用 ARTISTS：它是显式的多值字段，Picard 会为合作曲目逐项写入
    # （实际曲库里 `ARTISTS = G.E.M. 鄧紫棋` 与 `ARTISTS = Riot Games Music Team`
    # 是两条独立注释项），比拆分 ARTIST 的合并串更准确、更省猜测。
    # 没有该字段时回退到 ARTIST。
    raw_artists = tags.get("ARTISTS") or tags.get("ARTIST") or []
    artist_values = [v for v in raw_artists if v and v.strip()]
    artists: list[str] = []
    if artist_values:
        for value in artist_values:
            if _ARTIST_SEP.search(value):
                artists.extend(split_artists(value))
            else:
                artists.append(value.strip())
    else:
        artists = list(split_artists(first("ALBUMARTIST")))

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for a in artists:
        key = normalize_text(a)
        if key and key not in seen:
            seen.add(key)
            unique.append(a)

    # 版本标记同时取自标题与专辑名。实际 `漂向北方` 的版本标记
    # 只出现在专辑名 `漂向北方 (live)` 里，只看标题会漏掉。
    album_value = first("ALBUM")
    return MatchContext(
        title=title,
        artists=tuple(unique),
        album=album_value,
        duration=duration,
        version_tokens=extract_version_tokens(title) | extract_version_tokens(album_value),
        musicbrainz_trackid=first("MUSICBRAINZ_TRACKID") or None,
        isrc=first("ISRC") or None,
    )


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


class Decision(StrEnum):
    ACCEPT = "accept"
    NEEDS_REVIEW = "needs_review"
    NO_MATCH = "no_match"


@dataclass(frozen=True, slots=True)
class CandidateScore:
    provider: str
    provider_id: str
    score: float
    duration_delta: float | None
    title_score: float
    artist_score: float
    album_score: float
    version_ok: bool
    has_artists: bool = True
    """候选是否**提供了**艺人信息。

    存在的理由：`_artist_score` 在候选没有艺人时返回 0.0，于是艺人闸门
    被判失败——但「来源没给艺人」不等于「艺人不对」。实际 `07 春天里`
    的候选歌词完全正确（标题里就写着 G.E.M. 邓紫棋/方大同），
    却因艺人字段为空被永久拦在自动接受之外。

    把「没有证据」当成「证据表明有问题」，和把「没有证据」当成
    「证据表明安全」是同一类错误的两面。
    """
    provider_rank: int = 0
    """来源优先级，取自配置 `providers.enabled` 的顺序（越小越优先）。

    只用于**平分时**的决定，不参与打分——否则一个较差的 QQ 命中会压过
    更好的 LRCLIB 命中，那是另一种错误。
    """

    rejected: bool = False
    reject_reason: str | None = None
    details: tuple[str, ...] = ()


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


# 同一名字在不同来源里的书写差异：空格、点号、连字符、feat 标记等。
# 实际标签是 `G.E.M. 鄧紫棋`（有空格），QQ 返回 `G.E.M.邓紫棋`（无空格）。
# 只按原串比较会无谓丢分，因此对「去分隔符」的变体也做一次比较并取较优。
_SEP_CHARS = re.compile(r"[\s\.\-_·・,，、/／&＆+]+")


def _loose(value: str) -> str:
    return _SEP_CHARS.sub("", value)


def _best_ratio(a: str, b: str) -> float:
    """对原串与「去分隔符串」分别比较，取较优。"""
    best = _ratio(a, b)
    if best >= 1.0:
        return best
    la, lb = _loose(a), _loose(b)
    if la and lb and (la != a or lb != b):
        best = max(best, _ratio(la, lb))
    return best


def _title_score(ctx_title: str, cand_title: str) -> float:
    """标题相似度。

    基标题（去括号）全等给满分；否则用序列相似度，
    并对「一方包含另一方」给加成——异体字（漂/飘）会落在中等分值，
    由时长闸门兜底。
    """
    a = normalize_text(ctx_title)
    b = normalize_text(cand_title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_base = normalize_text(strip_bracketed(ctx_title))
    b_base = normalize_text(strip_bracketed(cand_title))
    if a_base and a_base == b_base:
        return 0.97
    base = _best_ratio(a_base or a, b_base or b)
    if a_base and b_base and (_loose(a_base) in _loose(b_base) or _loose(b_base) in _loose(a_base)):
        base = max(base, 0.85)
    # 全串也看一下，取较优，避免括号差异压低整体
    return max(base, _best_ratio(a, b) * 0.9)


#: 组合艺人串的分隔符。来源返回的形态很不统一：`A/B`、`A & B`、
#: `A、B`、`A feat. B`、`A with B` 都出现过。
_ARTIST_SPLIT = re.compile(
    r"[/、,;&]|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bvs\.?\b",
    re.IGNORECASE,
)
#: 括号内的别名。来源常在艺人名后附英文名或原名，
#: 如 `G.E.M. 邓紫棋(Gem Tang)`——实际这让相似度从 1.000 掉到 0.489，
#: 而门槛是 0.5，**差 0.011 就被判成「艺人不对」**。
_ARTIST_ALIAS = re.compile(r"[（(][^）)]*[）)]")


def _expand_artists(names: tuple[str, ...]) -> tuple[str, ...]:
    """把来源给的艺人串展开成规范化的单个艺人名。

    做两件事，都是实际遇到过的：

    1. **去掉括号别名**：`G.E.M. 邓紫棋(Gem Tang)` → `G.E.M. 邓紫棋`
    2. **拆分组合串**：`A/B`、`A & B`、`A feat. B` → `A`、`B`

    只在**艺人比较**时使用，不放进 `normalize_text`——
    那个函数对标题也在用，剥掉歌名里的括号会误伤
    （例如「春天里(Live)」的 Live 标记是版本证据）。

    去括号后若为空则保留原串，避免把纯括号的名字抹成空。
    """
    out: list[str] = []
    for raw in names:
        if not raw or not raw.strip():
            continue
        cleaned = _ARTIST_ALIAS.sub(" ", raw).strip() or raw.strip()
        for piece in _ARTIST_SPLIT.split(cleaned):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return tuple(out)


def _artist_score(ctx_artists: tuple[str, ...], cand_artists: tuple[str, ...]) -> float:
    """艺人集合相似度：每个本地艺人取与候选的最佳匹配，再取平均。

    但平均之外有一条**地板分**：只要任意一位本地歌手与候选高度吻合
    （≥0.9），分数就不低于 0.9。

    为什么地板分不能只看 `ctx_artists[0]`（旧实现）：
        歌手字段里的先后顺序是**标签写入方给的，不是可信的「主艺人」序**。
        实际 `我们的梦.flac` 的 ARTIST 是一条 20 人的署名串，`G.E.M.邓紫棋`
        排在第 8 位；候选正是 `G.E.M.邓紫棋` 本人也只能拿到 1/20 = 0.05 分，
        而第 0 位是谭咏麟，地板分永远不会触发——这道闸门对这条曲目形同虚设，
        那一首当年是靠收尾分支「分数达标就接受」蒙混过去的（见 `_decide_core`
        里补上的艺人硬约束）。

    为什么保留平均值而不是改成「候选侧吻合即通过」：
        平均是**更保守**的方向。合作曲目里有一位对不上时（本地 `A & B`、
        候选 `A & C`）应当扣分，不能靠单点吻合直接放行；而署名串很长这件事
        已经由地板分覆盖，不需要改动聚合方向。
    """
    if not ctx_artists or not cand_artists:
        return 0.0
    norm_cand = [normalize_text(a) for a in _expand_artists(cand_artists)]
    norm_cand = [a for a in norm_cand if a]
    if not norm_cand:
        return 0.0
    best_per_local = []
    for local in ctx_artists:
        nl = normalize_text(local)
        if not nl:
            continue
        best_per_local.append(max(_best_ratio(nl, nc) for nc in norm_cand))
    if not best_per_local:
        return 0.0
    avg = sum(best_per_local) / len(best_per_local)
    # 任一位高度吻合即给地板分（不限于第一位，理由见上）
    if max(best_per_local) >= 0.9:
        avg = max(avg, 0.9)
    return avg


def _artist_evidence_ok(
    ctx: MatchContext, best: CandidateScore, policy: MatchingPolicy
) -> tuple[bool, str | None]:
    """艺人证据是否成立。返回 (是否成立, 不成立时的说明)。

    口径必须与 `evaluate_gates` 里那一项**完全一致**，尤其
    「候选没提供艺人字段时闸门不适用」这一条：若这里只用
    `artist_score >= 阈值` 判断，就会把「来源没给这个字段」当成「艺人不对」，
    从而永久拦住歌词完全正确的候选（实际 `07 春天里` 遇到过）。
    抽成函数就是为了不让两处口径再漂移。
    """
    if not ctx.artists or not best.has_artists:
        return True, None
    if best.artist_score >= policy.auto_accept_min_artist_score:
        return True, None
    return False, (
        f"艺人相似度 {best.artist_score:.2f}，低于 {policy.auto_accept_min_artist_score}"
    )


def _content_evidence_ok(
    ctx: MatchContext, best: CandidateScore, policy: MatchingPolicy
) -> tuple[bool, list[str]]:
    """标题与艺人两项证据是否达标。返回 (是否达标, 未达标说明)。"""
    missing: list[str] = []
    if best.title_score < policy.auto_accept_min_title_score:
        missing.append(
            f"标题相似度 {best.title_score:.2f} 低于 {policy.auto_accept_min_title_score}"
        )
    artist_ok, artist_problem = _artist_evidence_ok(ctx, best, policy)
    if not artist_ok and artist_problem:
        missing.append(artist_problem)
    return (not missing), missing


def score_candidate(
    ctx: MatchContext,
    *,
    provider: str,
    provider_id: str,
    title: str,
    artists: tuple[str, ...],
    album: str,
    duration: float | None,
    policy: MatchingPolicy,
    provider_rank: int = 0,
) -> CandidateScore:
    """对一个候选打分。

    时长闸门是**硬约束**：超出容差直接标记 rejected。
    """
    details: list[str] = []

    # -- 时长闸门 --
    delta: float | None = None
    if ctx.duration is not None and duration is not None and duration > 0:
        delta = abs(duration - ctx.duration)
    duration_score = 0.0
    if delta is not None:
        if delta <= policy.max_duration_delta_seconds:
            # 0s → 1.0；正好卡在阈值 → 0.6
            duration_score = 1.0 - 0.4 * (delta / policy.max_duration_delta_seconds)
        else:
            return CandidateScore(
                provider=provider,
                provider_id=provider_id,
                score=0.0,
                duration_delta=delta,
                title_score=0.0,
                artist_score=0.0,
                album_score=0.0,
                version_ok=False,
                has_artists=bool([a for a in artists if a and a.strip()]),
                provider_rank=provider_rank,
                rejected=True,
                reject_reason=(
                    f"时长差 {delta:.1f}s 超出容差 {policy.max_duration_delta_seconds}s"
                ),
            )
    else:
        details.append("缺少时长证据（本地或候选），无法用时长闸门")

    t_score = _title_score(ctx.title, title)
    a_score = _artist_score(ctx.artists, artists)
    al_score = _ratio(normalize_text(ctx.album), normalize_text(album)) if ctx.album and album else 0.0

    # -- 版本一致性 --
    # 对称比较：只要**任一侧**带有影响录音的版本标记而另一侧没有或不同，
    # 就不能自动接受。明确要求「现场版、串烧、语言不明
    # 默认强制人工复核」，并禁止「把现场版退化为普通录音室查询后直接接受」。
    # 时长接近**不是**版本相同的证明。
    local_versions = recording_versions(ctx.version_tokens)
    cand_versions = recording_versions(extract_version_tokens(title))
    version_ok = local_versions == cand_versions
    if not version_ok:
        details.append(
            f"版本证据不一致: 本地 {sorted(local_versions) or '（无标记）'} "
            f"vs 候选 {sorted(cand_versions) or '（无标记）'}"
        )

    # -- 加权 --
    weights = {"duration": 0.40, "title": 0.35, "artist": 0.20, "album": 0.05}
    score = (
        weights["duration"] * duration_score
        + weights["title"] * t_score
        + weights["artist"] * a_score
        + weights["album"] * al_score
    )
    if not version_ok:
        score *= 0.6
        details.append("因版本不一致降权")

    return CandidateScore(
        provider=provider,
        provider_id=provider_id,
        score=round(score, 4),
        duration_delta=delta,
        title_score=round(t_score, 4),
        artist_score=round(a_score, 4),
        album_score=round(al_score, 4),
        version_ok=version_ok,
        has_artists=bool([a for a in artists if a and a.strip()]),
        provider_rank=provider_rank,
        rejected=False,
        reject_reason=None,
        details=tuple(details),
    )


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateCheck:
    """一道闸门的通过情况。

    为什么要把它显式化：复核者看到的必须是「**具体哪一道**闸没过、差多少」，
    而不是一句笼统的「置信度不足」。实际有个案例真实原因是
    「QQ 返回的是另一首歌，标题相似度 0.00」，而旧文案只说「置信度 0.573 低」——
    照着那句话去调低置信度门槛，就会把泡沫的歌词写进《如果没有你》。
    """

    key: str
    label: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Recommendation:
    """给复核者的采用建议。"""

    verdict: str
    """`adopt` 建议采用 / `reject` 建议不要采用 / `inspect` 需人工判断。"""
    text: str


def evaluate_gates(
    ctx: MatchContext, best: CandidateScore, policy: MatchingPolicy
) -> tuple[GateCheck, ...]:
    """逐项算出各闸门的通过情况，供决策与界面共用。"""
    delta = best.duration_delta
    checks = [
        GateCheck(
            "duration_gate", "时长差", delta is not None and delta <= policy.max_duration_delta_seconds,
            f"差 {delta:.2f}s，上限 {policy.max_duration_delta_seconds}s" if delta is not None else "候选无时长",
        ),
        GateCheck(
            "version", "版本证据", best.version_ok,
            "; ".join(best.details) if best.details else "本地与候选的版本标记一致",
        ),
        GateCheck(
            "title", "标题相似度", best.title_score >= policy.auto_accept_min_title_score,
            f"{best.title_score:.3f}，需 ≥{policy.auto_accept_min_title_score}",
        ),
        GateCheck(
            "artist", "艺人相似度",
            # 候选**没提供艺人**时本闸门不适用。把它判为失败等于用
            # 「来源没给这个字段」去否定一首明明正确的歌——实际会永久
            # 拦住歌词完全正确的候选。真正的保护来自标题闸门。
            (not ctx.artists) or (not best.has_artists)
            or best.artist_score >= policy.auto_accept_min_artist_score,
            (
                "候选未提供艺人信息，本闸门不适用"
                if (ctx.artists and not best.has_artists)
                else f"{best.artist_score:.3f}，需 ≥{policy.auto_accept_min_artist_score}"
            ),
        ),
        GateCheck(
            "confidence", "匹配总分", best.score >= policy.auto_accept_min_confidence,
            f"{best.score:.3f}，需 ≥{policy.auto_accept_min_confidence}",
        ),
    ]
    return tuple(checks)


def recommend(
    ctx: MatchContext, best: CandidateScore, policy: MatchingPolicy
) -> Recommendation:
    """根据打分给出可执行的建议。

    分两类情形：**内容对不上**（换来源，别采用）与**仅阈值卡边**（可以考虑采用）。
    这个区分必须做——否则复核者会一律点「接受」，那复核就失去意义。
    """
    # 标题几乎完全不同 = 这就是另一首歌。艺人/时长凑巧相近最容易骗过总分。
    if best.title_score < 0.5:
        return Recommendation(
            "reject",
            f"标题相似度仅 {best.title_score:.2f}，可能不是同一首歌。换一个来源试试。",
        )
    if not best.version_ok:
        return Recommendation(
            "inspect",
            "版本不一致，可能不是同一版本，请确认。",
        )
    if best.artist_score < policy.auto_accept_min_artist_score and ctx.artists:
        return Recommendation(
            "inspect",
            f"艺人相似度 {best.artist_score:.2f}，可能是翻唱或合唱版本。",
        )
    if best.title_score < policy.auto_accept_min_title_score:
        return Recommendation(
            "adopt",
            f"标题相似度 {best.title_score:.2f} 略低于阈值，"
            "常见原因是繁简体、括号注释或空格差异；其余证据（艺人/时长/版本）都吻合时"
            "通常可以采用。",
        )
    if best.score < policy.min_confidence and best.duration_delta is not None \
            and best.duration_delta <= policy.auto_accept_relaxed_seconds:
        return Recommendation(
            "adopt",
            f"时长差仅 {best.duration_delta:.2f}s，且标题与艺人均吻合，"
            "分数偏低主要来自专辑名不一致——一般可以采用。",
        )
    return Recommendation("inspect", "各项证据不齐，建议对比其它来源后决定。")


@dataclass(frozen=True, slots=True)
class MatchDecision:
    decision: Decision
    reason: str
    best: CandidateScore | None = None
    runner_up: CandidateScore | None = None
    checks: tuple[GateCheck, ...] = ()
    recommendation: Recommendation | None = None
    scored: tuple[CandidateScore, ...] = ()
    rejected: tuple[CandidateScore, ...] = field(default=())


def score_candidates(
    ctx: MatchContext,
    candidates: list[dict],  # noqa: ANN001 — 来自 Provider 的 Candidate，用鸭子类型避免循环依赖
    policy: MatchingPolicy,
    provider_order: Sequence[str] | None = None,
) -> list[CandidateScore]:
    """给全部候选打分。

    `provider_order` 是配置里声明的来源优先顺序（如 `["qq", "lrclib"]`）。
    它**只用于平分时的裁决**，不改变任何候选的分数——
    这样既能保证"主源优先"，又不会让较差的 QQ 命中压过更好的 LRCLIB 命中。
    """
    rank_of: dict[str, int] = {}
    if provider_order:
        rank_of = {name: idx for idx, name in enumerate(provider_order)}

    out: list[CandidateScore] = []
    for cand in candidates:
        provider = getattr(cand, "provider", "")
        out.append(
            score_candidate(
                ctx,
                provider=provider,
                provider_id=getattr(cand, "provider_id", ""),
                title=getattr(cand, "title", ""),
                artists=tuple(getattr(cand, "artists", ()) or ()),
                album=getattr(cand, "album", "") or "",
                duration=getattr(cand, "duration", None),
                policy=policy,
                provider_rank=rank_of.get(provider, len(rank_of)),
            )
        )
    return out


def decide(
    ctx: MatchContext,
    scores: list[CandidateScore],
    policy: MatchingPolicy,
) -> MatchDecision:
    """依据打分做最终决策，并在结果里附上**逐闸门判定**与**采用建议**。

    用包装而不是在每个 return 处补字段：决策分支有近十个，逐个改必然漏掉，
    而漏掉的那个恰恰就是用户会遇到的。这样任何路径返回的结果都带完整解释。
    """
    result = _decide_core(ctx, scores, policy)
    if result.best is None:
        return result
    return replace(
        result,
        checks=evaluate_gates(ctx, result.best, policy),
        recommendation=recommend(ctx, result.best, policy),
    )


def _decide_core(
    ctx: MatchContext,
    scores: list[CandidateScore],
    policy: MatchingPolicy,
) -> MatchDecision:
    """依据打分做最终决策。

    绝不「挑第一条」：低置信、歧义、版本冲突一律转复核。
    """
    accepted = [s for s in scores if not s.rejected]
    rejected = tuple(s for s in scores if s.rejected)

    # 排序口径：**分数优先，时长更近者优先**。
    #
    # 有意不把「来源」放在时长前面（默认）：按服务排序等于用品牌
    # 代替质量。来源只在分数与时长都相同时作为最后的稳定 tie-break。
    accepted.sort(
        key=lambda s: (
            -s.score,
            s.duration_delta if s.duration_delta is not None else 1e9,
            s.provider,
            s.provider_id,
        )
    )

    if not accepted:
        if rejected:
            return MatchDecision(
                decision=Decision.NO_MATCH,
                reason=f"全部 {len(rejected)} 个候选被硬约束否决",
                scored=tuple(),
                rejected=rejected,
            )
        return MatchDecision(decision=Decision.NO_MATCH, reason="没有候选")

    best = accepted[0]
    runner = accepted[1] if len(accepted) > 1 else None
    scored = tuple(accepted)

    # 版本证据不一致时，先看有没有**版本一致**的备选可用。
    # 时长相等不区分版本（实际 `喜欢你 (Live)` 与录音室版时长完全相同），
    # 所以版本冲突仍然不能自动写——那是"写错版本"的直接原因。
    # 但只要有一个版本一致的候选，就不必为最优候选的冲突去打扰人。
    if not best.version_ok:
        version_ok_options = [s for s in accepted if s.version_ok]
        if not version_ok_options:
            return MatchDecision(
                decision=Decision.NEEDS_REVIEW,
                reason="; ".join(best.details) or "版本证据不一致",
                best=best,
                runner_up=runner,
                scored=scored,
                rejected=rejected,
            )
        best = version_ok_options[0]
        rest = [s for s in accepted if s is not best]
        runner = rest[0] if rest else None
        scored = tuple([best, *rest])

    delta = best.duration_delta
    limit = policy.max_duration_delta_seconds

    # 「内容确实对得上」的独立下限，与总分无关。
    # 时长权重占 0.40，Δt=0 就能拿满；实际一个完全不相干的标题配上正确时长
    # 也能拿到 0.679 分。只按总分放行会写错歌词，所以标题与艺人相似度必须
    # 各自达标才能进入自动接受。
    content_ok, missing_evidence = _content_evidence_ok(ctx, best, policy)

    # 最优候选的**艺人证据**不成立时，改用后面「艺人证据成立」的备选。
    #
    # 为什么必须有这一步（实际案例，不是假设）：
    #   QQ 上存在**上传者冒名**的记录：标题与本地标签一字不差、时长也只差
    #   零点几秒，但歌手字段填的是上传者。按标题相似度排序它必然第一，
    #   于是艺人闸门判 0.00 分——闸门判得没错，**它确实是冒名的那一条**。
    #   而真正的那条（歌手字段正确）总分只排第二，永远轮不到。
    #   结果是：复核页把冒名记录当成「最优候选」展示，照着点「接受」会写错。
    #
    # 安全性：只在**已经通过时长闸门**（`accepted`）的候选里挑，并要求版本
    # 一致 + 艺人证据成立 + 标题至少 0.5（低于 0.5 就是另一首歌了，
    # 这是 `recommend` 里已经在用的同一条界线）。
    if ctx.artists and not _artist_evidence_ok(ctx, best, policy)[0]:
        alternatives = [
            s
            for s in accepted
            if s.version_ok
            and s.title_score >= 0.5
            and _artist_evidence_ok(ctx, s, policy)[0]
        ]
        if alternatives:
            previous = best
            best = alternatives[0]
            best = replace(
                best,
                details=best.details
                + (
                    "已跳过艺人证据不成立的最优候选"
                    f"（{previous.provider}/{previous.provider_id}，艺人相似度 "
                    f"{previous.artist_score:.2f}），改用本条",
                ),
            )
            rest = [s for s in accepted if s is not best]
            runner = rest[0] if rest else None
            scored = tuple([best, *rest])
            # 换了候选就必须重算 delta：下面的分档自动接受全按它判断。
            delta = best.duration_delta
            content_ok, missing_evidence = _content_evidence_ok(ctx, best, policy)

    # 艺人证据不成立且没有可用的备选时，到此为止。
    #
    # 为什么必须**在这里**拦住，而不是只让上面那个分档分支检查它：
    # 下面的收尾分支会在「分数达标 + 时长很准」时直接 ACCEPT，
    # **完全不看艺人证据**。也就是说一个艺人相似度 0.00 的候选只要总分够高，
    # 照样会被自动写进音乐文件——而 `evaluate_gates` 同时又在界面上把它标成
    # 「艺人相似度：未通过」。判据与结论互相矛盾，且矛盾的那一侧会写坏文件。
    #
    # 只对**艺人**下这道硬约束，不对标题：标题的异体字问题（本地「漂向北方」
    # 而上游写「飘向北方」，相似度 0.75）是真实且无解的，此时靠正确的歌手
    # 与吻合的时长命中是既定策略（见模块顶部第 1 条）。艺人不同则没有这种
    # 「写法差异」的正当理由——那是另一首歌或另一个演唱者。
    artist_ok, artist_problem = _artist_evidence_ok(ctx, best, policy)
    if not artist_ok:
        return MatchDecision(
            decision=Decision.NEEDS_REVIEW,
            reason=(
                f"{artist_problem}，且没有其他可用候选"
            ),
            best=best,
            runner_up=runner,
            scored=scored,
            rejected=rejected,
        )

    # ---- 分档自动接受：时长越准，对分数的要求越低 ----
    # 默认：这个操作不危险（不影响音频质量），不要过度保守；
    # 只有差异过大才该人工介入。
    if best.version_ok and content_ok and delta is not None:
        if (
            delta <= policy.auto_accept_within_seconds
            and best.score >= policy.auto_accept_min_confidence
        ):
            return MatchDecision(
                decision=Decision.ACCEPT,
                reason=(
                    f"时长差 {delta:.2f}s，分数 {best.score:.3f}，自动采用"
                ),
                best=best, runner_up=runner, scored=scored, rejected=rejected,
            )
        if (
            delta <= policy.auto_accept_relaxed_seconds
            and best.score >= policy.auto_accept_relaxed_confidence
        ):
            return MatchDecision(
                decision=Decision.ACCEPT,
                reason=(
                    f"时长差 {delta:.2f}s，分数 {best.score:.3f}，自动采用"
                ),
                best=best, runner_up=runner, scored=scored, rejected=rejected,
            )
        if (
            delta <= limit
            and best.score >= policy.auto_accept_wide_confidence
            and (runner is None or (best.score - runner.score) >= 0.05)
        ):
            return MatchDecision(
                decision=Decision.ACCEPT,
                reason=(
                    f"时长差 {delta:.2f}s，分数 {best.score:.3f}，明显优于次优，自动采用"
                ),
                best=best, runner_up=runner, scored=scored, rejected=rejected,
            )

    if best.score < policy.min_confidence:
        return MatchDecision(
            decision=Decision.NEEDS_REVIEW,
            reason=(
                f"最高分 {best.score:.3f}，低于阈值 {policy.min_confidence}"
                + (f"，时长差 {delta:.2f}s" if delta is not None else "")
            ),
            best=best,
            runner_up=runner,
            scored=scored,
            rejected=rejected,
        )

    if runner is not None and (best.score - runner.score) < policy.min_score_margin:
        # 用户策略：时长差很小就直接接受，不因「候选难分高下」送复核。
        #
        # 典型场景：LRCLIB 上同一首歌有多条元数据完全相同的记录，分数必然相等，
        # 但它们指向同一录音，判为歧义纯属浪费人工。
        #
        # 两条**不可**放宽的前提（否则会写错歌词）：
        #   - 版本证据必须一致。时长相等不区分版本：实际曲库里
        #     `喜欢你 (Live)` 与录音室版时长完全相同；`A.I.N.Y.` 更是反例——
        #     重生版时长差 0.20s，而正确的原版是 0.80s。
        #   - 置信度必须已达标（上面那道闸）。分数低说明标题/艺人本身对不上，
        #     那不是「候选接近」，而是「根本没匹配上」。
        if (
            best.version_ok
            and best.duration_delta is not None
            and best.duration_delta <= policy.auto_accept_within_seconds
        ):
            return MatchDecision(
                decision=Decision.ACCEPT,
                reason=(
                    f"时长差 {best.duration_delta:.2f}s ≤ "
                    f"{policy.auto_accept_within_seconds}s，"
                    f"按策略直接接受（最优 {best.score:.3f} 与次优 "
                    f"{runner.score:.3f} 接近，但版本证据一致且时长吻合）"
                ),
                best=best,
                runner_up=runner,
                scored=scored,
                rejected=rejected,
            )

        return MatchDecision(
            decision=Decision.NEEDS_REVIEW,
            reason=(
                f"候选接近（最优 {best.score:.3f} 与次优 {runner.score:.3f} "
                f"差 {best.score - runner.score:.3f} < {policy.min_score_margin}），"
                "且时长差超出自动接受范围"
            ),
            best=best,
            runner_up=runner,
            scored=scored,
            rejected=rejected,
        )

    return MatchDecision(
        decision=Decision.ACCEPT,
        reason=(
            f"置信度 {best.score:.3f}"
            + (f"，时长差 {best.duration_delta:.1f}s" if best.duration_delta is not None else "")
        ),
        best=best,
        runner_up=runner,
        scored=scored,
        rejected=rejected,
    )
