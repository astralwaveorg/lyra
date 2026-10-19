"""多来源歌词对比：把同一首歌在各来源上的候选与歌词并排取回，供人工选定。

## 为什么需要这个模块

用户反馈过一个真实问题：界面上只显示**胜出来源**，于是看到「LRCLIB」
就以为「QQ 没查到」——而实际上 QQ、LRCLIB、网易云常常**全都命中了**，
只是内容不同。复核队列的价值就在于**让人来选**，前提是能同时看到各来源的歌词。

## 设计取舍

- **逐源独立**：一个来源失败不影响其它来源的展示，错误逐源记录。
  否则网易云挂掉会让人误以为「三个源都没有」。
- **每源限量**：每个来源最多取回 `per_provider` 条候选的歌词。
  一次点击最多打出「来源数 × 2」个歌词请求，不会失控。
- **排序口径与自动决策一致**：分数降序，同分按配置声明的来源优先顺序。
  这样「界面里排第一的」就是「不干预时会自动选中的」，不会让人困惑。
- **被否决的也列出 1 条**：让人看到「有时长更近的候选但被版本闸拦下了」，
  这比只显示通过者更能解释系统为什么这么选。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from .config import Config, RootConfig
from .formats import read_audio_meta
from .lrc import parse_lrc, parse_lrc_lines
from .match import (
    Decision,
    build_context,
    decide,
    evaluate_gates,
    score_candidates,
)
from .pipeline import apply_cross_source_boost
from .providers.base import Provider, ProviderError, TrackQuery

__all__ = ["CompareOption", "build_comparison", "list_candidates"]


@dataclass(slots=True)
class CompareOption:
    """一个来源给出的一个可选歌词。"""

    provider: str = ""
    provider_id: str = ""
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    duration_delta: float | None = None
    score: float | None = None
    content: str = ""
    line_count: int = 0
    timestamp_count: int = 0
    synced: bool = False
    word_level: bool = False
    has_translation: bool = False
    rejected: bool = False
    reject_reason: str | None = None
    is_current: bool = False
    error: str | None = None
    _rank: int = field(default=0, repr=False)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("_rank", None)
        payload["artists"] = list(self.artists)
        payload["lines"] = [
            {"ms": ln.ms, "stamp": ln.stamp, "text": ln.text}
            for ln in parse_lrc_lines(self.content)
        ] if self.content else []
        return payload


def build_comparison(
    cfg: Config,
    *,
    root: RootConfig,
    rel_path: str,
    providers: Sequence[Provider],
    current_provider: str = "",
    current_provider_id: str = "",
    per_provider: int = 2,
) -> dict:
    """取回该曲目在各来源上的候选与歌词。只读，不修改任何文件或数据库。"""
    base = root.resolved_path
    path = base / rel_path
    meta = read_audio_meta(path, base)
    context = build_context(meta.tags, meta.info.duration)

    query = TrackQuery(
        title=context.title,
        artists=context.artists,
        album=context.album,
        duration=context.duration,
        musicbrainz_trackid=context.musicbrainz_trackid,
        isrc=context.isrc,
    )

    rank_of = {name: idx for idx, name in enumerate(cfg.providers.enabled)}
    options: list[CompareOption] = []

    # 第一步：所有来源都先搜一遍，把候选与分数**合在一起**。
    # 必须合起来才能应用跨源一致性加成——否则界面显示的分数与自动决策
    # 用的分数不一致，就会出现「排第一的却不是被选中的」这种困惑。
    searched: dict[str, tuple[list, dict]] = {}
    all_scores = []
    for provider in providers:
        try:
            candidates = provider.search(query)
        except ProviderError as exc:
            options.append(
                CompareOption(provider=provider.name, error=f"搜索失败：{exc}")
            )
            continue
        if not candidates:
            options.append(
                CompareOption(provider=provider.name, error="该来源没有搜到任何候选")
            )
            continue
        scores = score_candidates(
            context, candidates, cfg.policy.matching,
            provider_order=cfg.providers.enabled,
        )
        searched[provider.name] = (candidates, {c.provider_id: c for c in candidates})
        all_scores.extend(scores)

    # 与自动决策完全一致的口径：同一录音被多个来源命中时加成
    boosted = apply_cross_source_boost(all_scores) if all_scores else []

    # 第二步：按来源取回歌词
    for provider in providers:
        if provider.name not in searched:
            continue
        by_id = searched[provider.name][1]
        mine = [s for s in boosted if s.provider == provider.name]
        ranked = sorted(
            mine, key=lambda s: (-s.score, s.provider_rank, s.provider_id)
        )
        usable = [s for s in ranked if not s.rejected][:per_provider]
        rejected = [s for s in ranked if s.rejected][:1]
        picked = usable or rejected

        for score in picked:
            candidate = by_id.get(score.provider_id)
            option = CompareOption(
                provider=provider.name,
                provider_id=score.provider_id,
                title=candidate.title if candidate else "",
                artists=tuple(candidate.artists) if candidate else (),
                album=candidate.album if candidate else "",
                duration=candidate.duration if candidate else None,
                duration_delta=score.duration_delta,
                score=score.score,
                rejected=score.rejected,
                reject_reason=score.reject_reason,
                is_current=(
                    provider.name == current_provider
                    and score.provider_id == current_provider_id
                ),
                _rank=rank_of.get(provider.name, len(rank_of)),
            )
            if score.rejected:
                # 被否决的候选只列出来解释原因，不去取歌词——省一次请求
                option.error = score.reject_reason
                options.append(option)
                continue

            try:
                raw = provider.lyrics(candidate) if candidate is not None else None
            except ProviderError as exc:
                option.error = f"取歌词失败：{exc}"
                options.append(option)
                continue

            if raw is None:
                option.error = "该来源这首歌没有同步歌词"
            else:
                parsed = parse_lrc(raw.text)
                option.content = raw.text
                option.synced = raw.synced
                option.word_level = raw.word_level
                option.has_translation = bool(raw.translation)
                option.line_count = parsed.line_count
                option.timestamp_count = parsed.timestamp_count
            options.append(option)

    # 与自动决策同一口径：分数降序，同分按来源优先级
    options.sort(key=lambda o: (-(o.score or 0.0), o._rank, o.provider_id))

    return {
        "rel_path": rel_path,
        "track": {
            "title": context.title,
            "artists": list(context.artists),
            "album": context.album,
            "duration": context.duration,
            "version_tokens": sorted(context.version_tokens),
        },
        "provider_order": list(cfg.providers.enabled),
        "options": [o.to_dict() for o in options],
    }


def list_candidates(
    cfg: Config,
    *,
    root: RootConfig,
    rel_path: str,
    providers: Sequence[Provider],
    per_provider: int = 8,
) -> dict:
    """**只列候选，不取歌词**，供「先选来源、再选候选」的两步式交互使用。

    与 `build_comparison` 的关键区别：这里对每个来源只发**一次搜索**请求，
    不逐条取歌词。所以可以一次列出更多候选（默认每源 8 条），而代价仍然只是
    「来源数」次请求。歌词由使用者点中某一条时才单独取回。

    为什么要列出这么多、而不是只列 `build_comparison` 的那 2 条：
        实际真实失败案例里，**排第一的候选是上传者冒名记录**（标题与本地
        标签一字不差、时长也对，但歌手字段填的是上传者），真正的那一条分数
        排在后面；另有本地 TITLE 标签本身就被写坏（整个署名串塞进标题）的
        曲目，正确候选的标题相似度必然很低。这两种情况下，只列前 2 条等于
        让人**没得选**——而人工选歌词正是这两种情况唯一的出路。
    """
    base = root.resolved_path
    path = base / rel_path
    meta = read_audio_meta(path, base)
    context = build_context(meta.tags, meta.info.duration)

    query = TrackQuery(
        title=context.title,
        artists=context.artists,
        album=context.album,
        duration=context.duration,
        musicbrainz_trackid=context.musicbrainz_trackid,
        isrc=context.isrc,
    )

    searched: dict[str, dict[str, object]] = {}
    sources: list[dict] = []
    all_scores = []
    for provider in providers:
        entry: dict = {"provider": provider.name, "found": 0, "error": None}
        try:
            candidates = provider.search(query)
        except ProviderError as exc:
            entry["error"] = f"搜索失败：{exc}"
            sources.append(entry)
            continue
        entry["found"] = len(candidates)
        sources.append(entry)
        if not candidates:
            continue
        by_id = {c.provider_id: c for c in candidates}
        searched[provider.name] = by_id
        all_scores.extend(
            score_candidates(
                context, candidates, cfg.policy.matching,
                provider_order=cfg.providers.enabled,
            )
        )

    # 与自动决策完全一致的口径：跨来源一致加成，否则界面显示的分数与
    # 「不干预时系统会选谁」不一致。
    boosted = apply_cross_source_boost(all_scores) if all_scores else []
    decision = decide(context, list(boosted), cfg.policy.matching)
    best_id = (
        (decision.best.provider, decision.best.provider_id)
        if decision.best is not None else None
    )
    # 系统「自动接受」会采用的那一条（decision 里已经包含了跳过冒名候选、
    # 版本更一致的备选等全部策略），显式标出来，省得人去猜。
    chosen_id = best_id if decision.decision is Decision.ACCEPT else None

    rank_of = {name: idx for idx, name in enumerate(cfg.providers.enabled)}
    options: list[dict] = []
    for provider_name, by_id in searched.items():
        mine = [s for s in boosted if s.provider == provider_name]
        mine.sort(
            key=lambda s: (
                -(s.score or 0.0),
                s.duration_delta if s.duration_delta is not None else 1e9,
                s.provider_id,
            )
        )
        for score in mine[:per_provider]:
            candidate = by_id.get(score.provider_id)
            gates = evaluate_gates(context, score, cfg.policy.matching)
            options.append(
                {
                    "provider": provider_name,
                    "provider_id": score.provider_id,
                    "title": candidate.title if candidate else "",
                    "artists": list(candidate.artists) if candidate else [],
                    "album": candidate.album if candidate else "",
                    "duration": candidate.duration if candidate else None,
                    "duration_delta": score.duration_delta,
                    "score": score.score,
                    "title_score": score.title_score,
                    "artist_score": score.artist_score,
                    "version_ok": score.version_ok,
                    "has_artists": score.has_artists,
                    "rejected": score.rejected,
                    "reject_reason": score.reject_reason,
                    "is_best": (provider_name, score.provider_id) == best_id,
                    "is_chosen": (provider_name, score.provider_id) == chosen_id,
                    "provider_rank": rank_of.get(provider_name, len(rank_of)),
                    "gates": [
                        {"key": g.key, "label": g.label,
                         "passed": g.passed, "detail": g.detail}
                        for g in gates
                    ],
                }
            )

    # 全库统一排序：分数降序、同分按来源顺序。界面上「排第一的就是系统会选的」，
    # 但由于上面标了 is_chosen / is_best，人工改动不会被这两个标记误导。
    options.sort(key=lambda o: (-(o["score"] or 0.0), o["provider_rank"], o["provider_id"]))

    return {
        "rel_path": rel_path,
        "track": {
            "title": context.title,
            "artists": list(context.artists),
            "album": context.album,
            "duration": context.duration,
            "version_tokens": sorted(context.version_tokens),
        },
        "provider_order": list(cfg.providers.enabled),
        "sources": sources,
        "verdict": {
            "decision": decision.decision.value,
            "reason": decision.reason,
            "chosen_provider": chosen_id[0] if chosen_id else None,
            "chosen_provider_id": chosen_id[1] if chosen_id else None,
        },
        "options": options,
    }
