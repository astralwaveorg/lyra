"""AI 预审批处理：把「待审查」队列交给模型判定，高置信直接转为待写入。

为什么要有这个独立通道（重构方案 §4.3 的落地）：

- 抓取时的预审**逐首**消耗预算，批量扫描几千首时预算只够覆盖几首，
  待审查队列基本不会被消化；
- 待审查队列里的候选**本来就已经抓到了**，缺的只是「这个候选是不是
  同一首歌」的判定——判定所需的信息（候选标题/艺人/时长 + 本地元数据）
  重新搜一次就能拿到，不必重走整条抓取链路。

所以这里做的是：只处理**已经在待审查队列里**的条目 → 重建候选 → 模型判定
→ 高置信且程序闸门通过时转为「待写入」（origin='ai'），由随后的批量写入落盘。

判定留档（ai_json）、写入留痕（origin='ai' 走完整备份）、可撤销。
模型失败一律当「没有判断」，条目留在队列里。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ai import AIError, review_candidates
from .config import Config, RootConfig
from .db import StateStore
from .match import CandidateScore, MatchContext, decide, score_candidates
from .providers.base import Candidate, Provider

__all__ = ["PreReviewSummary", "run_ai_pre_review"]


@dataclass(slots=True)
class PreReviewSummary:
    """一次预审批处理的结果。"""

    scanned: int = 0
    """待审查队列里的条目数。"""
    judged: int = 0
    """实际调用模型判定的次数（受预算限制）。"""
    adopted: int = 0
    """判定通过、转为待写入的条目数。"""
    rejected: int = 0
    """模型明确判定不是同一首歌的条目数（保留在队列里供人工看）。"""
    errors: list[str] = field(default_factory=list)
    budget_exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "judged": self.judged,
            "adopted": self.adopted,
            "rejected": self.rejected,
            "errors": self.errors[:10],
            "budget_exhausted": self.budget_exhausted,
        }


def run_ai_pre_review(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    providers: dict[str, Provider],
    contexts: dict[int, tuple[MatchContext, str]],
    *,
    limit: int = 200,
    progress: Callable[[str], None] = lambda _m: None,
) -> PreReviewSummary:
    """对待审查队列做一次模型判定，高置信条目转为待写入。

    `contexts` 是 location_id → (MatchContext, revision_hash)，由调用方
    从审计结果构造（本模块不重复解析音频）。
    """
    summary = PreReviewSummary()
    if not cfg.ai.enabled or not cfg.ai.pre_review:
        return summary

    budget = max(0, int(cfg.ai.max_requests_per_run))
    rows = store.conn.execute(
        "SELECT s.id, s.location_id, s.provider, s.provider_id, s.content, "
        "s.score, s.duration_delta FROM staged_lyrics s "
        "WHERE s.state='needs_review' ORDER BY s.score DESC, s.created_at DESC "
        "LIMIT ?",
        (int(limit),),
    ).fetchall()
    summary.scanned = len(rows)
    if not rows:
        return summary

    for index, row in enumerate(rows, start=1):
        if budget <= 0:
            summary.budget_exhausted = True
            break
        location_id = int(row["location_id"])
        entry = contexts.get(location_id)
        if entry is None:
            continue
        context, _revision = entry
        provider = providers.get(str(row["provider"]))
        if provider is None:
            continue

        try:
            found = provider.search(context_query(context))
        except Exception as exc:  # noqa: BLE001 — 单条失败不影响其余
            summary.errors.append(f"{row['location_id']}: {type(exc).__name__}: {exc}"[:160])
            continue

        if not found:
            continue

        provider_order = list(providers.keys())
        scored = score_candidates(context, found, cfg.policy.matching, provider_order=provider_order)
        decision = decide(context, scored, cfg.policy.matching)
        if decision.best is None:
            continue

        budget -= 1
        summary.judged += 1
        try:
            judgement = review_candidates(
                protocol=cfg.ai.protocol,
                base_url=cfg.ai.base_url,
                model=cfg.ai.model,
                api_key=cfg.ai.api_key,
                timeout=cfg.ai.timeout_seconds,
                use_proxy=cfg.ai.use_proxy,
                track=_track_payload(context),
                candidates=_candidate_payloads(scored, found),
                max_delta_seconds=cfg.policy.matching.max_duration_delta_seconds,
            )
        except AIError as exc:
            summary.errors.append(f"{row['location_id']}: {exc}"[:160])
            continue

        payload = json.dumps(judgement.to_dict(), ensure_ascii=False)
        adoptable = (
            cfg.ai.auto_accept
            and judgement.best == f"{decision.best.provider}:{decision.best.provider_id}"
            and judgement.confidence >= cfg.ai.pre_review_confidence
            and (
                judgement.same_recording
                or (judgement.approximable and decision.approximate)
            )
        )
        store.mark_ai_reviewed(int(row["id"]), ai_json=payload, adopt=adoptable)
        if adoptable:
            summary.adopted += 1
        elif judgement.rejected:
            summary.rejected += 1
        if index % 10 == 0:
            progress(f"预审 {index}/{len(rows)}：采纳 {summary.adopted}")

    return summary


def context_query(context: MatchContext) -> Any:
    """把匹配上下文转成来源查询对象。"""
    from .providers.base import TrackQuery

    return TrackQuery(
        title=context.title,
        artists=tuple(context.artists),
        album=context.album,
        duration=context.duration,
        musicbrainz_trackid=context.musicbrainz_trackid,
        isrc=context.isrc,
    )


def _track_payload(context: MatchContext) -> dict[str, Any]:
    return {
        "filename": context.title,
        "dir_name": context.album,
        "title": context.title,
        "artists": list(context.artists),
        "album": context.album,
        "duration": context.duration,
        "notes": "来自待审查队列的候选判定",
    }


def _candidate_payloads(
    scored: list[CandidateScore], candidates: list[Candidate]
) -> list[dict[str, Any]]:
    by_key = {(c.provider, c.provider_id): c for c in candidates}
    out: list[dict[str, Any]] = []
    for entry in scored[:6]:
        cand = by_key.get((entry.provider, entry.provider_id))
        out.append({
            "id": f"{entry.provider}:{entry.provider_id}",
            "provider": entry.provider,
            "title": getattr(cand, "title", "") if cand else "",
            "artists": list(getattr(cand, "artists", ()) or ()) if cand else [],
            "album": getattr(cand, "album", "") if cand else "",
            "duration": getattr(cand, "duration", None) if cand else None,
            "synced": True,
            "excerpt": "",
        })
    return out
