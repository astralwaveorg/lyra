"""流水线：审计 → 建状态 → 抓取 → 匹配 → 暂存 →（可选）写入。

设计要点：

1. **抓取与写入分离**。`run_fetch` 只下载到服务数据目录，
   不碰音乐；`run_apply` 才走备份与事务。这样「自动刮削」可以放心常开，
   而写入仍受 `write_enabled` 与完整校验保护。
2. **来源熔断在「整轮」层面生效**。某个来源全局不可用时，如果对整批
   逐个重试，就是一次请求风暴。某个来源连续失败后，本轮直接停用它，
   但**其余来源与本地处理照常**。
3. **技术失败绝不记为「没有歌词」**。二者是不同的持久状态。
4. **负缓存不因重启或全量扫描而重置**；键包含音频身份、元数据版本、
   来源与策略版本，只靠 mtime 变化不足以失效。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import scan
from .audit import AuditResult, audit_root
from .config import Config, RootConfig
from .db import ResolutionOutcome, StateStore
from .formats import read_audio_meta, read_embedded_lyrics_texts
from .lang import ordered_providers, priority_group
from .lrc import content_hash, decode_lyrics_bytes, normalize_lrc, parse_lrc
from .match import (
    CandidateScore,
    Decision,
    MatchContext,
    build_context,
    decide,
    score_candidates,
)
from .models import IssueCode, LyricsState, MediaFormat
from .providers import (
    AuthRequiredError,
    BudgetExhaustedError,
    Candidate,
    Provider,
    ProviderError,
    RateLimitedError,
    RawLyrics,
    SchemaChangedError,
    TrackQuery,
    TransientNetworkError,
)
from .writer import (
    WritePlan,
    already_satisfied,
    apply_plan,
    default_sidecar_content,
    file_sha256,
    write_sidecar_atomic,
)

__all__ = [
    "POLICY_VERSION",
    "RunSummary",
    "TrackTarget",
    "run_fetch",
    "run_apply",
    "sync_state",
    "rescan",
    "fill_sidecars",
    "repair_sidecar_names",
    "clean_orphan_sidecars",
    "SidecarFillSummary",
    "compute_revision_hash",
]

POLICY_VERSION = "match-v1"

ProgressFn = Callable[[str], None]


def _noop(_: str) -> None:
    return


# ---------------------------------------------------------------------------
# 目标
# ---------------------------------------------------------------------------


def compute_revision_hash(tags: dict[str, list[str]], duration: float | None) -> str:
    """匹配相关元数据的版本指纹。

    只包含**参与匹配**的字段：歌名/歌手/专辑/时长。
    文件换路径、歌词变化、mtime 变化都不应让它失效，
    否则负缓存会被无意义地重置。
    """
    parts = [
        "|".join(tags.get("TITLE", [])),
        "|".join(tags.get("ARTIST", [])),
        "|".join(tags.get("ALBUM", [])),
        f"{duration:.3f}" if duration is not None else "",
    ]
    return content_hash("\u0001".join(parts))


@dataclass(slots=True)
class TrackTarget:
    location_id: int
    rel_path: str
    abs_path: Path
    context: MatchContext
    revision_hash: str
    media_format: MediaFormat
    asset_id: int | None = None
    tags: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class RunSummary:
    scanned: int = 0
    considered: int = 0
    fetched: int = 0
    accepted: int = 0
    needs_review: int = 0
    not_found: int = 0
    no_synced: int = 0
    skipped_cached: int = 0
    skipped_satisfied: int = 0
    skipped_excluded: int = 0
    applied: int = 0
    apply_failed: int = 0
    deferred_locked: int = 0
    """因数据库被占用而**保留待写入**的条数。

    与 apply_failed 分开统计很重要：failed 需要人介入，而 deferred_locked
    下一轮批量写入会自动续跑，属于「稍后重试」而不是「出了问题」。
    """
    yielded: bool = False
    """是否因为「人工在等写入」而提前收手。

    这不是失败：已处理的部分都已落盘，剩下的留到下一轮。
    """
    provider_disabled: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    considered_paths: list[str] = field(default_factory=list)
    """本次**实际处理过**的曲目相对路径。

    存在的理由：`limit` 原来只作用于抓取，写入阶段却遍历全部待写入项——
    传 `limit=5` 想小批量试写，结果会对整库动手。实际遇到过。
    有了这个清单，写入就能与抓取处理**同一批**曲目，语义才一致。
    """


# ---------------------------------------------------------------------------
# 状态同步
# ---------------------------------------------------------------------------


def sync_state(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    audit: AuditResult,
    *,
    full_scan: bool = False,
    progress: ProgressFn = _noop,
) -> list[TrackTarget]:
    """把一次审计结果落进状态库，并返回需要刮削的目标。

    `full_scan=True` 表示这次审计覆盖了整个受管根：此时**本次没见到的旧位置
    会被标记为缺失**。用户把目录改了个层级之后，库里会留下一堆指向已不存在
    路径的记录——它们会被当成「曲目」列在界面上（实际 423 条里 219 条是
    已删路径）。只补不删的同步是半个同步。
    """
    root_id = store.upsert_root(
        name=root.name,
        role=root.role.value,
        path=str(root.resolved_path),
        enabled=root.enabled,
        read_only=root.read_only,
    )
    store.set_root_health(root_id, "ok")

    targets: list[TrackTarget] = []

    # 移动/重命名识别：文件换路径后不该变成「旧行缺失 + 新行新建」两条记录。
    # 判据按可靠性排序：inode（同一文件系统内改名不变）→ 文件名 + 大小。
    # 只和**本次未见**的行比较，避免把一个还在原地的文件错配走。
    seen_rel = {t.audio.rel_path for t in audit.tracks}
    stale_by_inode: dict[int, int] = {}
    stale_by_name: dict[tuple[str, int], list[int]] = {}
    for row in store.conn.execute(
        "SELECT id, rel_path, size, inode FROM locations WHERE root_id=?", (root_id,)
    ).fetchall():
        if row["rel_path"] in seen_rel:
            continue
        if row["inode"]:
            stale_by_inode[int(row["inode"])] = int(row["id"])
        key = (str(row["rel_path"]).rsplit("/", 1)[-1], int(row["size"] or 0))
        stale_by_name.setdefault(key, []).append(int(row["id"]))

    def _relocate(track) -> None:
        """若这个文件是「换了路径的旧文件」，把旧行改到新路径。"""
        nonlocal moved
        if track.audio.rel_path in existing_paths:
            return
        inode = track.audio.inode or None
        if not inode:
            try:
                inode = int(track.audio.path.stat().st_ino) or None
            except OSError:
                inode = None
        hit = stale_by_inode.pop(inode, None) if inode else None
        if hit is None:
            key = (track.audio.path.name, int(track.audio.size or 0))
            bucket = stale_by_name.get(key) or []
            hit = bucket.pop(0) if bucket else None
        if hit is None:
            return
        store.update_location_path(hit, track.audio.rel_path)
        existing_paths.add(track.audio.rel_path)
        moved += 1

    existing_paths = {
        str(row["rel_path"])
        for row in store.conn.execute(
            "SELECT rel_path FROM locations WHERE root_id=?", (root_id,)
        )
    }
    moved = 0

    for track in audit.tracks:
        info = track.audio
        if info.parse_error is not None or not info.media_format.supported:
            continue

        _relocate(track)
        location_id = store.upsert_location(
            root_id=root_id,
            rel_path=info.rel_path,
            size=info.size,
            mtime=info.mtime,
            # 必须真的把 inode 写进库：`_relocate` 靠它识别改名，
            # 而这一列长期是 NULL，等于那条路径从来没执行过。
            inode=info.inode or None,
            ctime=info.ctime or None,
            full_hash=None,
        )
        ctx = build_context(track.tags, info.duration)
        revision_hash = compute_revision_hash(track.tags, info.duration)

        sidecar_path = track.sidecar.path
        store.record_lyric_observation(
            location_id=location_id,
            embedded_state=track.embedded.state.value,
            embedded_fields=track.embedded.fields,
            embedded_hash=(
                track.embedded.parsed.normalized_hash if track.embedded.parsed else None
            ),
            sidecar_state=track.sidecar.state.value if track.sidecar.exists else None,
            sidecar_hash=(
                track.sidecar.parsed.normalized_hash if track.sidecar.parsed else None
            ),
            sidecar_size=track.sidecar.size if track.sidecar.exists else None,
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            sidecar_names_differ=IssueCode.SIDECAR_NAME_MISMATCH in track.issues,
            has_embedded_cover=track.embedded.has_cover,
            issues=[i.value for i in track.issues],
        )

        targets.append(
            TrackTarget(
                location_id=location_id,
                rel_path=info.rel_path,
                abs_path=info.path,
                context=ctx,
                revision_hash=revision_hash,
                media_format=info.media_format,
                tags=track.tags,
            )
        )

    if full_scan:
        # 只标记、不删除：文件可能是被临时移走/挂载掉线。
        seen = {t.location_id for t in targets}
        stale = [
            row["id"]
            for row in store.conn.execute(
                "SELECT id FROM locations WHERE root_id=? AND missing_since IS NULL",
                (root_id,),
            )
            if row["id"] not in seen
        ]
        for location_id in stale:
            store.mark_location_missing(int(location_id))
        if stale:
            progress(f"另有 {len(stale)} 个位置本次未见，已标记为缺失")
        if moved:
            progress(f"识别到 {moved} 个文件被移动/重命名，已更新记录")

    progress(f"状态已登记：{len(targets)} 个目标")

    # 观测刚刷新完 → 顺手清掉「这一首已经有歌词了」的过期复核记录。
    #
    # 为什么放在这里而不是只放在写入之后：曲库里的歌词可能是**别处**写进去的
    # （人工用别的工具编辑、或上一轮的写入），队列里的旧候选照样会变成噪声。
    # 每次审计都清一遍，队列里留下的就一定是「真的还需要决定」的。
    try:
        cleaned = store.supersede_obsolete_reviews()
    except Exception:  # noqa: BLE001 — 清理失败不影响审计结果
        cleaned = 0
    if cleaned:
        progress(f"清理过期复核记录 {cleaned} 条")
    return targets


# ---------------------------------------------------------------------------
# 规则与负缓存
# ---------------------------------------------------------------------------


def _excluded(store: StateStore, target: TrackTarget) -> str | None:
    """返回排除原因；None 表示未被排除。"""
    now = time.time()
    for rule in store.iter_rules(now=now):
        scope = rule["scope"]
        target_value = rule["target"]
        if scope == "path" and target_value == target.rel_path:
            return f"规则 {rule['id']}（{rule['action']}）"
        if scope == "global" and target_value == "*":
            return f"全局规则 {rule['id']}（{rule['action']}）"
    return None


def resolution_key(target: TrackTarget, provider: str) -> str:
    """负缓存键：音频身份 + 元数据版本 + 来源 + 策略版本。

    有意**不含** mtime 或路径，这样换路径、改歌词不会重置负缓存。
    """
    identity = target.asset_id if target.asset_id is not None else target.rel_path
    return f"{identity}:{target.revision_hash}:{provider}:{POLICY_VERSION}"


def _negative_cache_hit(
    store: StateStore, target: TrackTarget, provider: str, now: float
) -> bool:
    row = store.get_resolution(resolution_key(target, provider))
    if row is None:
        return False
    if row["outcome"] not in (
        ResolutionOutcome.NOT_FOUND,
        ResolutionOutcome.NO_SYNCED_LYRICS,
    ):
        return False
    nxt = row["next_retry_at"]
    return nxt is not None and nxt > now


def next_retry_at(outcome: str, attempts: int, cfg: Config, now: float) -> float | None:
    """按结果类型计算下次重试时间。

    技术失败**不消耗**负结果轮次。
    """
    if outcome == ResolutionOutcome.NOT_FOUND:
        seq = cfg.retry.not_found_intervals_days
    elif outcome == ResolutionOutcome.NO_SYNCED_LYRICS:
        seq = cfg.retry.no_synced_intervals_days
    elif outcome == ResolutionOutcome.TRANSIENT_FAILED:
        seq_s = cfg.retry.transient_backoff_seconds
        if not seq_s:
            return None
        index = min(max(attempts - 1, 0), len(seq_s) - 1)
        return now + seq_s[index]
    else:
        return None

    if not seq:
        return None
    if attempts > len(seq):
        # 复查计划用尽 → 暂停，等待人工或相关变化
        return None
    return now + seq[min(max(attempts - 1, 0), len(seq) - 1)] * 86400


# ---------------------------------------------------------------------------
# 交叉确认
# ---------------------------------------------------------------------------


def apply_cross_source_boost(
    scores: Sequence[CandidateScore], *, bonus: float = 0.08, min_each: float = 0.8
) -> list[CandidateScore]:
    """两个不同来源同时给出高置信且时长一致的候选 → 提升置信度。

    单一来源的「相似度最高」不能替代确认；
    两个独立来源在时长上一致，是比任何单一分数都强的证据。
    """
    strong = [s for s in scores if not s.rejected and s.score >= min_each]
    providers = {s.provider for s in strong}
    if len(providers) < 2:
        return list(scores)

    tight = [
        s for s in strong
        if s.duration_delta is not None and s.duration_delta <= 1.0
    ]
    if len({s.provider for s in tight}) < 2:
        return list(scores)

    return [
        replace(s, score=round(min(s.score + bonus, 1.0), 4), details=(*s.details, "双源交叉确认"))
        if (not s.rejected and s.provider in {t.provider for t in tight})
        else s
        for s in scores
    ]


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def _resolve_tie_by_content(
    providers: dict[str, Provider],
    candidates: Sequence[Candidate],
    decision,
    store: StateStore,
    cfg: Config,
    target: TrackTarget,
) -> RawLyrics | None:
    """同分候选造成的「假歧义」：取回歌词比对内容来消解。

    实际 LRCLIB 上同一首歌常有多条**元数据完全相同**的记录（重复条目），
    仅凭分数无法区分，会被保守地送进复核。但把它们各自的歌词取回来比较，
    若内容一致，就说明选哪条都一样——这是比分数更强的证据。

    要求**至少两条独立取回且内容完全一致**才确认；
    只有一条取到歌词不能算一致（那只是「另一条没有歌词」）。
    """
    if decision.runner_up is None:
        return None

    top = [s for s in decision.scored[:3] if not s.rejected]
    if len(top) < 2:
        return None

    fetched: list[RawLyrics] = []
    for score in top:
        provider = providers.get(score.provider)
        if provider is None:
            continue
        raw, _err = _fetch_lyrics(provider, candidates, score, store, cfg, target)
        if raw is not None:
            fetched.append(raw)

    if len(fetched) < 2:
        return None

    digests = {content_hash(normalize_lrc(r.text)) for r in fetched}
    if len(digests) == 1:
        return fetched[0]
    return None


def _has_confident_candidate(
    ctx: MatchContext,
    candidates: Sequence[Candidate],
    cfg: Config,
    provider_order: Sequence[str] | None = None,
) -> bool:
    """该来源是否已经给出「足以直接采用」的候选（用于早退）。

    判据与自动接受的最严档一致：时长差 ≤ `auto_accept_within_seconds`、
    分数 ≥ `min_confidence`、版本证据一致、标题与艺人相似度各自达标。

    为什么宁严勿宽：早退唯一的收益是省掉后续来源的查询与歌词请求。
    如果为了快而放宽判据，就会用「其实还有更好来源」的曲目去换速度，
    那是本末倒置——用户的目的是**把歌词补全且正确**。
    """
    if not candidates:
        return False
    policy = cfg.policy.matching
    scores = score_candidates(
        ctx, list(candidates), policy,
        provider_order=list(provider_order) if provider_order else cfg.providers.enabled,
    )
    for score in scores:
        if score.rejected or not score.version_ok:
            continue
        if score.duration_delta is None:
            continue
        if score.duration_delta > policy.auto_accept_within_seconds:
            continue
        if score.score < policy.min_confidence:
            continue
        if score.title_score < policy.auto_accept_min_title_score:
            continue
        if ctx.artists and score.artist_score < policy.auto_accept_min_artist_score:
            continue
        return True
    return False


def _provider_summary(
    scores: Sequence[CandidateScore], *, limit: int = 6
) -> list[dict[str, object]]:
    """按来源汇总各自的最佳候选，让「哪些源命中过」对用户可见。

    为什么需要它：只记录胜出来源会让人误以为「另一个源没查到」。
    实际 `Still Breathing` 在 QQ 与 LRCLIB 上都命中了（QQ 时长还更准），
    但结果只记了 LRCLIB，看起来就像 QQ 从未被查询。把各源的最佳候选一并
    记下来并展示，才能让来源透明、也才能在复核时对比取舍。
    """
    best_by_provider: dict[str, CandidateScore] = {}
    for s in scores:
        current = best_by_provider.get(s.provider)
        if current is None or s.score > current.score:
            best_by_provider[s.provider] = s

    ordered = sorted(best_by_provider.values(), key=lambda s: (-s.score, s.provider_rank))
    return [
        {
            "provider": s.provider,
            "provider_id": s.provider_id,
            "score": s.score,
            "duration_delta": s.duration_delta,
            "version_ok": s.version_ok,
            "rejected": s.rejected,
            "reason": s.reject_reason,
        }
        for s in ordered[:limit]
    ]


#: 进度上报的最小间隔。必须短于「人开始怀疑卡住」的阈值——
#: 实际 10 分钟不变的文字足以让人（包括我）判断成死锁。
_PROGRESS_INTERVAL_SECONDS = 3.0


def _human_seconds(seconds: float) -> str:
    """把秒数变成人读的时长。"""
    if seconds < 0:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _describe_target(target: TrackTarget) -> str:
    """用曲名+艺人描述当前正在处理的曲目，而不是只报一个序号。

    使用者认的是歌名，不是第几号。文件没有标题时退回文件名。
    """
    title = (target.context.title or "").strip()
    if not title:
        return Path(target.rel_path).name
    artists = "/".join(a for a in (target.context.artists or ()) if a)
    return f"{title} — {artists}" if artists else title


def run_fetch(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    providers: Sequence[Provider],
    *,
    audit: AuditResult | None = None,
    limit: int | None = None,
    force: bool = False,
    only_rel_paths: set[str] | None = None,
    artist: str = "",
    progress: ProgressFn = _noop,
    should_yield: Callable[[], bool] | None = None,
) -> RunSummary:
    """抓取歌词到暂存区。**不写入任何音乐文件。**

    `should_yield` 由监控传入：每当有人在等写入租约就返回 True，
    循环在**当前曲目处理完之后**停下，把锁让给人工操作。

    `only_rel_paths` 用于监听事件触发的增量批次：只处理这些文件。

    `artist` 按艺人筛选曲目（**包含**即命中，不要求完全相等）：
    一个文件可能有多个艺人，选中其中任意一个都算命中。
    """
    started = time.monotonic()
    summary = RunSummary()

    audit = audit or audit_root(root, only_rel_paths=only_rel_paths)
    targets = sync_state(
        store, cfg, root, audit,
        full_scan=only_rel_paths is None, progress=progress,
    )
    if artist.strip():
        targets = [
            t for t in targets
            if _artist_matches(artist.strip(), t.context.artists)
        ]
    summary.scanned = len(targets)

    by_name = {p.name: p for p in providers}
    disabled: dict[str, str] = {}

    # 进度按**时间**节流，不按条数。
    # 实际教训：原来写 `considered % 10 == 0`，而每首要约 20 秒（三个来源
    # 各带限流间隔），于是界面十分钟里一直显示「已处理 30 首」——
    # 看起来和卡死没有区别，我自己都被骗去查了两轮「是否卡住」。
    # 进度信息的全部意义就是让人知道「还在动」，所以必须带上当前项与预计剩余。
    total_targets = len(targets)
    last_report = 0.0

    def _report(target, done: int) -> None:
        nonlocal last_report
        now = time.monotonic()
        if now - last_report < _PROGRESS_INTERVAL_SECONDS and done < total_targets:
            return
        last_report = now
        elapsed = now - started
        line = f"{done}/{total_targets} · {_describe_target(target)} · 已用 {_human_seconds(elapsed)}"
        if done >= 3:
            remaining = elapsed / done * (total_targets - done)
            line += f" · 预计剩余 {_human_seconds(remaining)}"
        progress(line)

    for _index, target in enumerate(targets):
        if limit is not None and summary.considered >= limit:
            break
        if should_yield is not None and should_yield():
            summary.yielded = True
            break

        # -- 排除 --
        reason = _excluded(store, target)
        if reason:
            summary.skipped_excluded += 1
            continue

        # -- 已有有效同步歌词则跳过（无收益，只有风险）--
        if (
            cfg.policy.skip_if_valid_synced
            and not force
            and _has_valid_synced(store, target)
        ):
            summary.skipped_satisfied += 1
            continue

        summary.considered += 1
        summary.considered_paths.append(target.rel_path)
        _report(target, summary.considered)

        _fetch_one(store, cfg, target, by_name, disabled, summary)

    summary.provider_disabled = disabled
    # 记录表滚动清理：只保留最近 100 条，避免几个月后变成几万行。
    # 放在批量写入结束时做，开销与「一次删除扫描」相当。
    try:  # noqa: SIM105
        store.prune_audit()
    except Exception:  # noqa: BLE001 — 清理失败不影响写入结果
        pass  # noqa: SIM105

    summary.elapsed_seconds = time.monotonic() - started
    return summary


def _has_valid_synced(store: StateStore, target: TrackTarget) -> bool:
    row = store.conn.execute(
        "SELECT embedded_state, sidecar_state FROM lyric_observations "
        "WHERE location_id=? ORDER BY observed_at DESC LIMIT 1",
        (target.location_id,),
    ).fetchone()
    if row is None:
        return False
    states = {row["embedded_state"], row["sidecar_state"]}
    return LyricsState.SYNCED.value in states


def _artist_matches(wanted: str, artists: Sequence[str]) -> bool:
    """选中的艺人是否命中这首曲目的艺人。

    口径是**包含**：选「The Beatles」应当命中「The Beatles / Tony Sheridan」
    这样的多艺人署名；反过来也一样（选一个更长的串也应当命中）。
    比较前先做规范化（大小写、空白、标点差异不算不同人）。
    """
    from .match import normalize_text

    target = normalize_text(wanted)
    if not target:
        return True
    for artist in artists:
        name = normalize_text(artist)
        if not name:
            continue
        if name == target or target in name or name in target:
            return True
    return False


def _fetch_one(
    store: StateStore,
    cfg: Config,
    target: TrackTarget,
    providers: dict[str, Provider],
    disabled: dict[str, str],
    summary: RunSummary,
) -> None:
    now = time.time()
    query = TrackQuery(
        title=target.context.title,
        artists=target.context.artists,
        album=target.context.album,
        duration=target.context.duration,
        musicbrainz_trackid=target.context.musicbrainz_trackid,
        isrc=target.context.isrc,
    )

    all_candidates: list[Candidate] = []
    query_log: list[dict[str, object]] = []
    any_cached = False

    # 按语种决定来源查询顺序：华语走 QQ 优先，外语走 LRCLIB 优先。
    # 顺序同时决定「早退时优先信谁」与「同分时谁排前面」。
    group = priority_group(
        target.context.title,
        target.context.artists,
        cjk_uses_zh_priority=cfg.providers.cjk_uses_zh_priority,
    )
    provider_order = ordered_providers(
        list(providers.keys()),
        {
            "zh": cfg.providers.priority_zh,
            "foreign": cfg.providers.priority_foreign,
        },
        group,
    )
    query_log.append({"priority_group": group, "order": provider_order})

    for name in provider_order:
        provider = providers[name]
        if name in disabled:
            continue
        if _negative_cache_hit(store, target, name, now):
            any_cached = True
            continue
        try:
            found = provider.search(query)
        except (RateLimitedError, TransientNetworkError) as exc:
            # 来源级失败：本轮停用它，避免对全库逐个重试形成请求风暴
            disabled[name] = f"{type(exc).__name__}: {exc}"
            store.record_resolution(
                resolution_key=resolution_key(target, name),
                provider=name,
                outcome=ResolutionOutcome.TRANSIENT_FAILED,
                asset_id=target.asset_id,
                location_id=target.location_id,
                next_retry_at=next_retry_at(
                    ResolutionOutcome.TRANSIENT_FAILED, 1, cfg, now
                ),
            )
            continue
        except SchemaChangedError as exc:
            disabled[name] = f"响应结构变化: {exc}"
            store.record_resolution(
                resolution_key=resolution_key(target, name),
                provider=name,
                outcome=ResolutionOutcome.SCHEMA_FAILED,
                asset_id=target.asset_id,
                location_id=target.location_id,
            )
            continue
        except AuthRequiredError as exc:
            disabled[name] = f"需要登录: {exc}"
            store.record_resolution(
                resolution_key=resolution_key(target, name),
                provider=name,
                outcome=ResolutionOutcome.AUTH_REQUIRED,
                asset_id=target.asset_id,
                location_id=target.location_id,
            )
            continue
        except BudgetExhaustedError:
            # 预算用尽但仍有未评估候选 → 不能声称已查无结果
            any_cached = True
            continue
        except ProviderError as exc:
            summary.errors.append(f"{target.rel_path} [{name}] {type(exc).__name__}: {exc}")
            continue

        entry: dict[str, object] = {"provider": name, "candidates": len(found)}
        query_log.append(entry)
        all_candidates.extend(found)

        # ---- 早退：一个来源已给出足够好的结果，就不再查其余来源 ----
        # 默认：不必每个平台都查。实际三个来源全查一遍要 10~15 秒；
        # 绝大多数曲目第一个来源就能给出时长差 <1s 的命中。
        # 判据有意不放松（见 _has_confident_candidate）：早退只用来省时间，
        # 绝不用来降低质量。
        if _has_confident_candidate(target.context, found, cfg, provider_order):
            entry["early_exit"] = True
            break

    if not all_candidates:
        if any_cached:
            summary.skipped_cached += 1
            return
        # 所有来源都正常响应但没有候选 → 真正的负结果
        _record_negative(store, cfg, target, providers.keys() - disabled.keys(),
                         ResolutionOutcome.NOT_FOUND, now, query_log)
        summary.not_found += 1
        return

    scores = score_candidates(
        target.context, all_candidates, cfg.policy.matching,
        provider_order=provider_order,
    )
    boosted = apply_cross_source_boost(scores)
    decision = decide(target.context, boosted, cfg.policy.matching)
    by_provider = _provider_summary(boosted)

    if decision.decision is Decision.NO_MATCH:
        _record_negative(store, cfg, target, providers.keys() - disabled.keys(),
                         ResolutionOutcome.NOT_FOUND, now, query_log,
                         providers_summary=by_provider)
        summary.not_found += 1
        return

    # 同分候选的假歧义：用歌词内容一致性消解（比分数更强的证据）
    resolved_lyrics: RawLyrics | None = None
    if decision.decision is Decision.NEEDS_REVIEW:
        resolved_lyrics = _resolve_tie_by_content(
            providers, all_candidates, decision, store, cfg, target
        )
        if resolved_lyrics is not None:
            decision = replace(
                decision,
                decision=Decision.ACCEPT,
                reason=decision.reason + "；但前若干候选的歌词内容完全一致，视为确认",
            )

    # 取歌词：按分数从高到低找**第一个带时间轴**的候选。
    #
    # 为什么不是「只取最优候选」：最优候选的歌词可能没有时间轴
    # （LRCLIB 同一首歌的多条记录，有的有同步歌词、有的只有纯文本）。
    # 旧实现遇到这种情况就整首歌放弃——分数更高的候选白白浪费，
    # 而下面那条有时间轴的候选根本没被试过。
    best = decision.best
    assert best is not None
    if resolved_lyrics is not None:
        lyrics, lyrics_error = resolved_lyrics, None
        used = best
    else:
        lyrics, used, lyrics_error = _lyrics_with_timeline(
            providers, all_candidates, boosted, store, cfg, target
        )
        if used is not None and used is not best:
            # 换了候选就要重算依据：分数、时长差、来源都可能不同。
            best = used
            decision = replace(
                decision,
                best=used,
                reason=decision.reason + f"；改用有时间轴的候选（{used.provider}）",
            )

    if lyrics is None:
        outcome = (
            ResolutionOutcome.NO_SYNCED_LYRICS
            if lyrics_error is None
            else ResolutionOutcome.TRANSIENT_FAILED
        )
        _record_negative(
            store, cfg, target, {best.provider}, outcome, now, query_log,
            provider_id=best.provider_id,
        )
        if outcome is ResolutionOutcome.NO_SYNCED_LYRICS:
            summary.no_synced += 1
        return

    text = normalize_lrc(lyrics.text)
    digest = content_hash(text)

    # 决策为复核时也暂存，让复核者能离线看歌词
    store.stage_lyrics(
        location_id=target.location_id,
        content_hash=digest,
        content=text,
        provider=lyrics.provider,
        provider_id=lyrics.provider_id,
        score=best.score,
        duration_delta=best.duration_delta,
        state="staged" if decision.decision is Decision.ACCEPT else "needs_review",
    )

    outcome = (
        ResolutionOutcome.MATCHED
        if decision.decision is Decision.ACCEPT
        else ResolutionOutcome.NEEDS_REVIEW
    )
    store.record_resolution(
        resolution_key=resolution_key(target, best.provider),
        provider=best.provider,
        outcome=outcome,
        asset_id=target.asset_id,
        location_id=target.location_id,
        provider_id=best.provider_id,
        candidate={
            "title": best.provider_id,
            "score": best.score,
            "duration_delta": best.duration_delta,
            "reason": decision.reason,
            "runner_up_score": decision.runner_up.score if decision.runner_up else None,
            # 各来源的命中情况：让「QQ 也命中了」这件事可见，而不是只记胜出者
            "providers": by_provider,
            # 逐闸门判定 + 采用建议：复核者需要的是「具体哪一道没过」，
            # 而不是一句笼统的「置信度不足」——照着后者去调低门槛会写错歌词。
            "checks": [
                {"key": c.key, "label": c.label, "passed": c.passed, "detail": c.detail}
                for c in decision.checks
            ],
            "recommendation": (
                {"verdict": decision.recommendation.verdict,
                 "text": decision.recommendation.text}
                if decision.recommendation else None
            ),
        },
        lyrics_hash=digest,
        query_log=query_log,
    )

    summary.fetched += 1
    if decision.decision is Decision.ACCEPT:
        summary.accepted += 1
    else:
        summary.needs_review += 1


def _fetch_lyrics(
    provider: Provider,
    candidates: Sequence[Candidate],
    best: CandidateScore,
    store: StateStore,
    cfg: Config,
    target: TrackTarget,
) -> tuple[RawLyrics | None, str | None]:
    """按 (provider, provider_id) 取回歌词并做结构校验。

    只有**可解析的同步歌词**才被接受；纯文本或占位文案一律不算成功，
    否则会被自己的 clear_plain_embedded 策略立刻清掉，形成死循环。
    """
    match = next(
        (c for c in candidates if c.provider == best.provider and c.provider_id == best.provider_id),
        None,
    )
    if match is None:
        return None, "候选消失"

    try:
        raw = provider.lyrics(match)
    except (RateLimitedError, TransientNetworkError, SchemaChangedError, AuthRequiredError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except ProviderError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if raw is None or not raw.text.strip():
        return None, None
    if not raw.synced:
        return None, None

    parsed = parse_lrc(raw.text, duration_seconds=target.context.duration)
    if parsed.state is not LyricsState.SYNCED:
        # suspicious 也拒绝自动写入：占位文案、越界、全零时间都不能落盘
        return None, None
    return raw, None


#: 找「有时间的歌词」时最多试几个候选。前几个通常就够（同一个来源
#: 的不同版本），再多只是白白消耗上游请求。
_TIMELINE_FALLBACK_CANDIDATES = 5


def _lyrics_with_timeline(
    providers: dict[str, Provider],
    candidates: Sequence[Candidate],
    scores: Sequence[CandidateScore],
    store: StateStore,
    cfg: Config,
    target: TrackTarget,
    *,
    limit: int = _TIMELINE_FALLBACK_CANDIDATES,
) -> tuple[RawLyrics | None, CandidateScore | None, str | None]:
    """按分数顺序找**第一个带时间轴**的候选并取回它的歌词。

    为什么不能只看最优候选：
        最优候选的歌词**可能没有时间轴**。LRCLIB 上同一首歌有多条记录，
        有的版本有同步歌词，有的只有纯文本。实际 `07 Kansas City.flac`：
        最优的网易候选（0.86）只有纯文本，于是整首歌**什么都没拿到**——
        而分数第二的 LRCLIB 候选（0.98）是有时间轴的。

    所以这里按分数从高到低逐个试，取第一个带时间轴的。这样「推荐 =
    最高分」与「结果都必须有时间轴」就不再互相打架。

    返回 (歌词, 用到的候选, 错误说明)。
    """
    ordered = sorted(
        scores, key=lambda s: (-s.score, s.duration_delta if s.duration_delta is not None else 1e9)
    )
    last_error: str | None = None
    tried = 0
    for score in ordered:
        if score.rejected:
            continue
        provider = providers.get(score.provider)
        if provider is None:
            continue
        tried += 1
        if tried > limit:
            break
        lyrics, error = _fetch_lyrics(
            provider, candidates, score, store, cfg, target
        )
        if lyrics is not None:
            return lyrics, score, None
        if error:
            last_error = error
    return None, None, last_error


def _record_negative(
    store: StateStore,
    cfg: Config,
    target: TrackTarget,
    providers: Iterable[str],
    outcome: str,
    now: float,
    query_log: Sequence[dict[str, object]],
    *,
    provider_id: str | None = None,
    providers_summary: Sequence[dict[str, object]] | None = None,
) -> None:
    for name in providers:
        key = resolution_key(target, name)
        row = store.get_resolution(key)
        attempts = (int(row["attempts"]) + 1) if row is not None else 1
        store.record_resolution(
            resolution_key=key,
            provider=name,
            outcome=outcome,
            asset_id=target.asset_id,
            location_id=target.location_id,
            provider_id=provider_id,
            candidate=(
                {"providers": list(providers_summary)} if providers_summary else None
            ),
            query_log=query_log,
            next_retry_at=next_retry_at(outcome, attempts, cfg, now),
        )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _sidecar_target(audio_path: Path, observed: Path | None) -> Path:
    """决定这次写入该落到哪个 .lrc。

    观测到的配对**不能无条件照用**：观测是上一次审计的结果，可能已经过期，
    也可能本来就配错了。实际踩到的坑：`The Beatles/As Nature Intended` 里
    三首同名的 `I've Got a Feeling`（04 / 07 / 14），14 号一度把
    04 号的 lrc 当成自己的——写歌词时把内容写进了**别人**的文件，
    自己那一份始终「缺 .lrc」，而界面不报错（因为「内容已相同」被判成功）。

    规则：
      - 观测到的就是本文件自己的同名 lrc → 用它；
      - 观测到的名字**正好是同目录另一个音频的同名 lrc** → 那是别人的，
        不用；改用本文件自己的 `<同名>.lrc`；
      - 其余情况（异名配对，如 `09 艺人 - 让你知道.flac` ↔ `让你知道.lrc`）
        → 仍然照用观测结果，否则同一首歌会多出一份歌词文件。
    """
    own = audio_path.with_suffix(".lrc")
    if observed is None:
        return own
    if observed.stem == audio_path.stem:
        return observed
    # 观测到的 lrc 是不是同目录另一个音频的「自己的那份」？
    for ext in scan.AUDIO_EXTENSIONS:
        sibling = observed.with_name(observed.stem + ext)
        if sibling.is_file() and sibling != audio_path:
            return own
    return observed


def _apply_one(
    store: StateStore,
    cfg: Config,
    base: Path,
    backup_dir: Path,
    row,
    summary: RunSummary,
    *,
    force: bool = False,
) -> bool:
    """写入单个文件的歌词，返回 True 表示确实写入了。

    抽成独立函数是为了让调用方能做**逐文件失败隔离**：捕获异常后继续下一首，
    而不是让一个瞬时错误终止整批。

    `force` 表示这是**人工明确选定**的写入：放宽「纯文本歌词不算成功」
    这类自动策略的复核要求（见 `WritePlan.force`）。
    """
    abs_path = base / row["rel_path"]
    if not abs_path.is_file():
        store.set_staged_state(row["id"], "missing")
        return False

    meta = read_audio_meta(abs_path, base)
    if meta.info.parse_error is not None or not meta.info.media_format.supported:
        store.set_staged_state(row["id"], "unsupported")
        return False

    # sidecar 目标必须用「审计时配对到的那一条」。
    # 真实库里有 `09 艺人 - 让你知道.flac` ↔ `让你知道.lrc` 这种异名配对；
    # 若这里仍按精确同名去写，同一首歌会出现两份歌词文件。
    observed = store.conn.execute(
        "SELECT sidecar_path FROM lyric_observations "
        "WHERE location_id=? AND sidecar_path IS NOT NULL "
        "ORDER BY observed_at DESC LIMIT 1",
        (row["location_id"],),
    ).fetchone()
    sidecar_target = _sidecar_target(
        abs_path,
        Path(observed["sidecar_path"]) if observed else None,
    )

    # 来源标记：auto 是系统自动接受，manual 是人工在界面上选定。
    origin = row["origin"] if "origin" in row.keys() else "auto"  # noqa: SIM118

    plan = WritePlan(
        location_id=row["location_id"],
        audio_path=abs_path,
        sidecar_path=sidecar_target,
        media_format=meta.info.media_format,
        lyrics_text=row["content"],
        embed=cfg.policy.embed_lyrics,
        write_sidecar=cfg.policy.write_sidecar_lrc,
        # 自动接受的不备份（见 WriteConfig.backup_review_only）：
        # 全库整份复制代价十几 GB，而自动接受写错概率低。
        needs_backup=not (cfg.write.backup_review_only and origin == "auto"),
        original_full_hash=file_sha256(abs_path),
        original_audio_hash="",
        original_tags=dict(meta.tags),
        reason=f"provider={row['provider']} id={row['provider_id']}",
        force=force,
    )
    if already_satisfied(plan):
        store.set_staged_state(row["id"], "already_satisfied")
        summary.skipped_satisfied += 1
        # 人工选定的那一条**本来就在文件里**：目标状态已经达成，
        # 这是成功而不是「什么都没做」。批量流程里保持原样（跳过即可），
        # 人工流程由调用方按 `skipped_satisfied` 判定成功。
        return False

    outcome = apply_plan(store, plan, cfg, backup_dir=backup_dir)
    if not outcome.ok:
        store.set_staged_state(row["id"], "failed")
        summary.apply_failed += 1
        summary.errors.append(f"{row['rel_path']}: {outcome.error}")
        return False

    store.set_staged_state(
        row["id"], "already_satisfied" if outcome.noop else "applied"
    )
    store.conn.execute(
        "INSERT INTO approvals(location_id, lyrics_hash, approved_by, "
        "policy_version, approved_at) VALUES(?,?,?,?,?)",
        (
            row["location_id"],
            row["content_hash"],
            # 人工在界面上选定的那一条要记成 user：审计时能分清
            # 「系统自动接受」与「人看过之后决定」。
            "user" if origin == "manual" else "auto",
            POLICY_VERSION,
            time.time(),
        ),
    )
    return True


@dataclass(slots=True)
class SidecarFillSummary:
    """「把内嵌歌词补成 .lrc」的结果。"""

    considered: int = 0
    filled: int = 0
    already: int = 0
    """已经有合格 .lrc（带时间轴），无需动作。"""
    untimed: int = 0
    """有 .lrc 但内容无时间轴——**不覆盖**，那是别人写的文件。"""
    no_source: int = 0
    """内嵌里也没有可用的带时间轴歌词。"""
    failed: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered, "filled": self.filled,
            "already": self.already, "untimed": self.untimed,
            "no_source": self.no_source, "failed": self.failed,
            "dry_run": self.dry_run, "errors": list(self.errors),
        }


def _pick_synced_text(texts: Sequence[str]) -> str | None:
    """从多份内嵌歌词里挑出**带时间轴**且行数最多的那一份。"""
    best: str | None = None
    best_lines = -1
    for text in texts:
        if not text.strip():
            continue
        parsed = parse_lrc(text)
        if parsed.state is not LyricsState.SYNCED:
            continue
        if parsed.line_count > best_lines:
            best, best_lines = text, parsed.line_count
    return best


def _lrc_with_headers(text: str, tags: dict[str, list[str]]) -> str:
    """把内嵌正文落成 .lrc 内容：头部补 [ti]/[ar]/[al]，正文规范化。

    补头部是因为库里的 .lrc 都带这三行（来源本来就会给），
    没有头部的外挂歌词在播放器里看不出是哪首歌的。
    """
    body = default_sidecar_content(text).strip("\n")
    if not body:
        return ""

    def first(key: str) -> str:
        values = tags.get(key) or []
        return str(values[0]).strip() if values else ""

    head = []
    for key, name in (("TITLE", "ti"), ("ARTIST", "ar"), ("ALBUM", "al")):
        value = first(key)
        if value:
            head.append(f"[{name}:{value}]")
    return "\n".join([*head, body]) + "\n"


def fill_sidecars(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    only_rel_paths: set[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: ProgressFn = _noop,
) -> SidecarFillSummary:
    """把**内嵌已有、但没有 .lrc** 的曲目补出同名 `.lrc`。

    ## 这条路径解决的是什么

    库里大量文件在整理时就把带时间轴的歌词写进了内嵌标签（Picard/发行方
    自带的 LRC）。而播放器、车机、网易云本地歌词**只认外挂 .lrc**。
    抓取层看到「已有有效同步歌词」就直接跳过（`skip_if_valid_synced`），
    于是这些文件永远拿不到 .lrc——实际新增 176 首后，
    抓取结果是「接受 0／写入 0」，而磁盘上只有 15 个 .lrc。

    正确的做法不是再联网抓一遍（内容已经在文件里，重抓只会引入差异），
    而是把内嵌的那份**原样落成外挂文件**：不联网、不改音频、字节零风险。

    ## 安全边界

    - 只在 `.lrc` 缺失或为空占位时创建；**有内容的一律不覆盖**
      （`overwrite=False`，冲突只报告）。别人写的纯文本歌词也算「有内容」。
    - 受 `policy.write_sidecar_lrc` / `policy.fill_empty_lrc` / `write_enabled`
      与根的 `read_only` 共同约束。
    - 不改音频，因此天然满足「音频数据逐字节不变」。
    """
    summary = SidecarFillSummary(dry_run=dry_run)
    if not (cfg.policy.write_sidecar_lrc and cfg.policy.fill_empty_lrc):
        summary.errors.append(
            "policy.write_sidecar_lrc 或 policy.fill_empty_lrc 为关，未处理"
        )
        return summary
    if not dry_run and not cfg.policy.write_enabled:
        summary.errors.append("write_enabled=false：全库只读，未写入")
        return summary
    if not dry_run and root.read_only:
        summary.errors.append(f"受管根 {root.name} 标记为只读，未写入")
        return summary

    audit = audit_root(root, only_rel_paths=only_rel_paths)
    written: set[str] = set()

    for track in audit.tracks:
        info = track.audio
        if info.parse_error is not None or not info.media_format.supported:
            continue
        if track.embedded.state is not LyricsState.SYNCED:
            summary.no_source += 1
            continue
        sidecar = track.sidecar
        if sidecar.state is LyricsState.SYNCED:
            summary.already += 1
            continue
        # 有内容但不是时间轴格式：那是别人写的文件，不覆盖
        if sidecar.exists and (sidecar.size or 0) > 0 \
                and sidecar.state is not LyricsState.SYNCED:
            summary.untimed += 1
            continue

        texts, err = read_embedded_lyrics_texts(info.path)
        best = _pick_synced_text(texts)
        if best is None:
            summary.no_source += 1
            if err:
                summary.errors.append(f"{info.rel_path}: {err}")
            continue
        content = _lrc_with_headers(best, track.tags)
        if not content:
            summary.no_source += 1
            continue

        if limit is not None and summary.considered >= limit:
            progress(f"已达本轮上限 {limit}，其余留到下一轮")
            break
        summary.considered += 1
        if dry_run:
            summary.filled += 1
            progress(f"将补 .lrc：{info.rel_path}")
            continue

        target = sidecar.path if sidecar.exists else info.path.with_suffix(".lrc")
        try:
            result = write_sidecar_atomic(
                target, content, txn_id=uuid.uuid4().hex[:12], overwrite=False,
            )
        except Exception as exc:  # noqa: BLE001 — 单个失败不影响其余
            summary.failed += 1
            summary.errors.append(f"{info.rel_path}: {type(exc).__name__}: {exc}")
            continue
        if result.written:
            summary.filled += 1
            written.add(info.rel_path)
            progress(f"已补 .lrc：{info.rel_path}")
        else:
            summary.already += 1

    if written and not dry_run:
        # 写完立刻重读这几个文件，让界面与列表当场反映（不等 30 秒对账）
        try:
            refreshed = audit_root(root, only_rel_paths=written)
            sync_state(store, cfg, root, refreshed)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(
                "写入已完成，但刷新界面状态失败（下次对账会自愈）："
                f"{type(exc).__name__}: {exc}"
            )
    return summary


#: 封面图片的「规范名」判定：这些词干 + 图片扩展名会被统一成小写。
#:
#: 为什么统一成小写：播放器与服务端（Emby / Jellyfin / Kodi / Navidrome）
#: 查找封面时优先按 `cover.jpg`、`folder.jpg` 这类小写名去匹配；而 Linux
#: 文件系统区分大小写，`Cover.jpg` 与 `cover.jpg` 可以同时存在——
#: 两张不同的图并存时，不同播放器会挑到不同的那张。
_COVER_STEMS = frozenset({
    "cover", "folder", "front", "album", "albumart", "albumartsmall",
    "folderart", "coverart", "artwork", "thumb",
})
_COVER_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def normalize_cover_names(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    dry_run: bool = False,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """把封面图片的文件名统一成小写（`Cover.JPG` → `cover.jpg`）。

    只改**文件名的大小写**，不动内容、不改扩展名、不跨目录。规则：

    - 词干在 `_COVER_STEMS` 里、扩展名是图片：整体小写化，例如
      `Cover.jpg`、`FOLDER.PNG`、`Front.Jpeg`。
    - 目标名已存在且是**另一个文件**：把非规范名的那份**移入回收站**，
      保留规范名的那份。封面可以重新上传/下载，不值得为它排一次人工确认；
      万一是更好的那张，回收站里也能原样还原。
    - 目标名已存在但是**同一个文件**（macOS/Windows 的大小写不敏感卷）：
      走临时名中转完成「只改大小写」。
    - 每次真实改名都记进 `renames`，日志页可以逐条撤销。
    """
    from . import scan
    from .fileops import move_to_trash

    base = root.resolved_path
    result: dict[str, Any] = {
        "renamed": 0,
        "removed": 0,
        "skipped": 0,
        "errors": [],
        "items": [],
        "removals": [],
        "dry_run": dry_run,
    }
    if not base.is_dir():
        return result

    # 改名记录需要 root_id；监控里它一定已存在，但手动调用时可能还没登记。
    root_id = store.upsert_root(
        name=root.name, role=root.role.value, path=str(base),
        enabled=root.enabled, read_only=root.read_only,
    )
    batch = time.strftime("%Y%m%d-%H%M%S")
    for path in scan._walk(base, root):
        suffix = path.suffix.lower()
        if suffix not in _COVER_EXTS or path.stem.lower() not in _COVER_STEMS:
            continue
        target_name = path.stem.lower() + suffix
        if path.name == target_name:
            result["skipped"] += 1
            continue
        try:
            rel_old = str(path.relative_to(base))
        except ValueError:
            continue
        target = path.with_name(target_name)
        try:
            rel_new = str(target.relative_to(base))
        except ValueError:
            continue

        same = target.exists() and _same_file(path, target)
        if target.exists() and not same:
            # 同名小写文件已在：删掉非规范名的那份（移入回收站，可还原）。
            if dry_run:
                result["removals"].append({"from": rel_old, "kept": rel_new})
                result["removed"] += 1
                continue
            try:
                moved = move_to_trash(cfg, root, path, batch)
            except OSError as exc:
                result["errors"].append(f"{rel_old}: {type(exc).__name__}: {exc}")
                continue
            result["removals"].append(
                {"from": rel_old, "kept": rel_new, "moved_to": moved}
            )
            result["removed"] += 1
            progress(f"封面重名：移除 {path.name}（保留 {target_name}）")
            continue

        if dry_run:
            result["items"].append({"from": rel_old, "to": rel_new})
            result["renamed"] += 1
            continue

        try:
            if same:
                # 大小写不敏感卷上的「只改大小写」：先挪到临时名再改回来，
                # 否则 os.rename 可能认为源和目标相同而不做任何事。
                temp = path.with_name(f".lyra-case-{uuid.uuid4().hex[:8]}")
                os.rename(path, temp)
                os.rename(temp, target)
            else:
                os.rename(path, target)
        except OSError as exc:
            result["errors"].append(f"{rel_old}: {type(exc).__name__}: {exc}")
            continue

        result["items"].append({"from": rel_old, "to": rel_new})
        result["renamed"] += 1
        progress(f"封面改名：{path.name} → {target_name}")
        try:
            store.record_rename(
                root_id=root_id,
                old_rel_path=rel_old,
                new_rel_path=rel_new,
                reason="封面文件名规范化",
                media_format="image",
            )
        except Exception as exc:  # noqa: BLE001 — 记录失败不能回滚已完成的改名
            result["errors"].append(f"{rel_old}: 记录改名失败 {type(exc).__name__}: {exc}")

    return result


def clean_orphan_sidecars(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    dry_run: bool = False,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """清理「确实多余」的孤儿 .lrc：内容重复的、以及曲库里没有对应音频的。

    判定分三档，**只有前两档会动文件**：

    1. `duplicate` 且孤儿那份的**歌词行** 100% 被现有 .lrc 覆盖
       （作词/作曲/标题行不计）→ 内容重复，移入回收站；
    2. `unmatched` 且**整个曲库里**没有任何音频的名字与它相关
       （互相包含都算）→ 没有对应音频，移入回收站；
       曲库里另有同名音频（跨目录）的不动——那可能是同一首歌的另一个版本，
       也可能是这个文件放错了目录，留给人看；
    3. 其余（歧义多候选、内容有差异）→ 不动。

    同名但名字不同的情况不在这里处理，归 `repair_sidecar_names` 改名。

    动作全部是**移入回收站**（保留相对路径、可还原），不是删除。
    """
    from .ai import lyric_lines, orphan_coverage
    from .audit import find_orphan_sidecars
    from .fileops import move_to_trash
    from .scan import iter_audio_files, normalize_stem

    base = root.resolved_path
    if not base.is_dir():
        return {"cleaned": 0, "kept": 0, "errors": ["受管根不存在"], "items": []}

    # 全库音频的规范化名字：判断「曲库里到底有没有这首歌」
    library_stems = {
        normalize_stem(p.stem) for p in iter_audio_files(base, root) if normalize_stem(p.stem)
    }

    cleaned: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    errors: list[str] = []
    batch = time.strftime("%Y%m%d-%H%M%S")

    for item in find_orphan_sidecars(root):
        path = base / item["rel_path"]
        reason = ""
        if item["action"] == "duplicate" and item.get("paired_sidecar"):
            existing = base / item["paired_sidecar"]
            try:
                inside = orphan_coverage(
                    decode_lyrics_bytes(existing.read_bytes())[0],
                    decode_lyrics_bytes(path.read_bytes())[0],
                )
            except OSError as exc:
                errors.append(f"{item['rel_path']}: {exc}")
                continue
            if inside >= 0.999 and lyric_lines(
                decode_lyrics_bytes(path.read_bytes())[0]
            ):
                _action, reason = "cleanup", "内容与现有的 .lrc 重复"
            else:
                kept.append({"path": item["rel_path"],
                             "why": "内容有差异（多出歌词行），保留"})
                continue
        elif item["action"] == "unmatched":
            stem = normalize_stem(Path(item["rel_path"]).stem)
            related = [
                other for other in library_stems
                if len(stem) >= 2 and (stem in other or other in stem)
            ]
            if related:
                kept.append({"path": item["rel_path"],
                             "why": "曲库里有同名音频（可能在别的目录），保留"})
                continue
            if not lyric_lines(decode_lyrics_bytes(path.read_bytes())[0]):
                kept.append({"path": item["rel_path"], "why": "没有歌词内容，保留"})
                continue
            _action, reason = "cleanup", "曲库里没有对应音频"
        else:
            same_dir = [
                c for c in item.get("candidates", [])
                if c.get("scope") == "same_dir"
            ]
            why = ("同目录有多个候选音频，无法判断" if len(same_dir) > 1
                   else "曲库里有同名音频（在别的目录），保留")
            kept.append({"path": item["rel_path"], "why": why})
            continue

        if dry_run:
            cleaned.append({"path": item["rel_path"], "why": reason})
            continue
        try:
            move_to_trash(cfg, root, path, batch)
        except OSError as exc:
            errors.append(f"{item['rel_path']}: {type(exc).__name__}: {exc}")
            continue
        cleaned.append({"path": item["rel_path"], "why": reason})
        progress(f"已清理：{item['rel_path']}（{reason}）")

    return {
        "cleaned": len(cleaned), "kept": len(kept),
        "errors": errors, "items": cleaned, "kept_items": kept,
        "dry_run": dry_run, "trash_batch": None if dry_run else batch,
    }


def repair_sidecar_names(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    only_paths: set[str] | None = None,
    dry_run: bool = False,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """把「已配对但名字不同」的 `.lrc` 改成音频同名。

    为什么必须做：Lyra 自己认得异名 sidecar（`pair_sidecars` 会按规范化名字
    配对），但**播放器不认**——它们只找 `<音频同名>.lrc`。实际库里
    `01 - 张国荣 - 童年时.lrc` 与 `01 童年時.flac` 是同一首歌，Lyra 能配上，
    换到车机上就"没有歌词"。

    安全边界：只改已经配对成功的那一份；目标名已存在（另一份歌词）时跳过，
    绝不覆盖。改名是原地 rename，内容一字不动。
    """
    audit = audit_root(root, only_rel_paths=only_paths)
    base = root.resolved_path
    renamed: list[dict[str, str]] = []
    skipped = 0
    errors: list[str] = []
    touched: set[str] = set()

    for track in audit.tracks:
        if IssueCode.SIDECAR_NAME_MISMATCH not in track.issues:
            continue
        if not track.sidecar.exists or track.sidecar.path is None:
            continue
        src = track.sidecar.path
        target = track.audio.path.with_suffix(".lrc")
        if src == target:
            continue
        if target.exists():
            # 已经有同名 .lrc：这是「两份歌词」，留给人决定
            skipped += 1
            continue
        if dry_run:
            renamed.append({"from": str(src.relative_to(base)),
                            "to": str(target.relative_to(base))})
            continue
        try:
            os.rename(src, target)
        except OSError as exc:
            errors.append(f"{track.audio.rel_path}: {type(exc).__name__}: {exc}")
            continue
        renamed.append({"from": str(src.relative_to(base)),
                        "to": str(target.relative_to(base))})
        touched.add(track.audio.rel_path)
        progress(f"已改名：{src.name} → {target.name}")

    if touched and not dry_run:
        try:
            refreshed = audit_root(root, only_rel_paths=touched)
            sync_state(store, cfg, root, refreshed)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                "改名已完成，但刷新界面状态失败（下次对账会自愈）："
                f"{type(exc).__name__}: {exc}"
            )
    return {
        "renamed": len(renamed), "skipped": skipped,
        "errors": errors, "items": renamed, "dry_run": dry_run,
    }


def repair_orphan_sidecars(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    only_paths: set[str] | None = None,
    dry_run: bool = False,
    progress: ProgressFn = _noop,
) -> dict[str, Any]:
    """把「名字与音频不一致、但确实是同一首」的孤儿 .lrc 改成音频同名。

    实际触发场景：`01 - 张国荣 - 童年时.lrc` 与 `01 童年時.flac` 在同一目录，
    前者是下载来的歌词、后者是曲库里的音频。繁简与「艺人 - 」前缀让字面不同，
    于是这份 lrc 被报成「不属于任何曲目」——而它其实就该叫 `01 童年時.lrc`。

    安全边界（一条都不能松）：
    - 只处理**孤儿**：当前没有任何音频归属它；
    - 目标必须是**同一目录**里的音频，且它当前没有合格 .lrc；
    - 新名字已存在且非空 → 拒绝，绝不覆盖别人的歌词；
    - 依据只有「规范化后的名字」（含繁简折叠）与双向唯一，不猜内容。
    """
    from .audit import find_orphan_sidecars

    base = root.resolved_path
    if not base.is_dir():
        return {"renamed": 0, "skipped": 0, "errors": ["受管根不存在"], "items": []}

    findings = find_orphan_sidecars(root)
    renamed: list[dict[str, str]] = []
    skipped = 0
    errors: list[str] = []
    touched: set[str] = set()

    for item in findings:
        if only_paths is not None and item["rel_path"] not in only_paths:
            continue
        if item["action"] == "duplicate":
            skipped += 1
            continue
        candidate = next(
            (c for c in item.get("candidates", [])
             if c["scope"] == "same_dir" and c["paired_sidecar"] is None),
            None,
        )
        if candidate is None:
            skipped += 1
            continue
        src = base / item["rel_path"]
        target = (base / candidate["rel_path"]).with_suffix(".lrc")
        if not src.is_file():
            skipped += 1
            continue
        if target.exists() and target.stat().st_size > 0:
            # 目标已经有内容：这是「同一首歌的两份歌词」，该由人决定留哪份
            skipped += 1
            continue
        if dry_run:
            renamed.append({"from": item["rel_path"], "to": str(target.relative_to(base))})
            continue
        try:
            os.rename(src, target)
        except OSError as exc:
            errors.append(f"{item['rel_path']}: {type(exc).__name__}: {exc}")
            continue
        renamed.append({"from": item["rel_path"], "to": str(target.relative_to(base))})
        touched.add(candidate["rel_path"])
        progress(f"已配对：{item['rel_path']} → {target.name}")

    if touched and not dry_run:
        try:
            refreshed = audit_root(root, only_rel_paths=touched)
            sync_state(store, cfg, root, refreshed)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                "改名已完成，但刷新界面状态失败（下次对账会自愈）："
                f"{type(exc).__name__}: {exc}"
            )
    return {
        "renamed": len(renamed), "skipped": skipped,
        "errors": errors, "items": renamed, "dry_run": dry_run,
    }


def rescan(store: StateStore, cfg: Config, root: RootConfig) -> dict[str, object]:
    """整库状态对账：审计 + 全量同步，**不联网、不抓歌词、不写文件**。

    这是「界面反映磁盘现状」的唯一入口：移动/重命名会被识别并更新路径，
    删除会被标记缺失，新增会被登记。监控进程每 30 秒跑一次。

    返回值里带 `added_paths`：**新增（或换了路径）的具体文件**。
    只有计数是不够的——监控拿到计数之后没法对这些文件做事，于是
    「新增音乐自动补 .lrc / 自动刮削」永远只能等下一次文件事件；
    实际批量拷入 176 首之后，事件只覆盖了其中一部分，
    其余一直停在「有内嵌、无 .lrc」。
    """
    root_row = store.conn.execute(
        "SELECT id FROM roots WHERE name=?", (root.name,)
    ).fetchone()
    root_id = int(root_row["id"]) if root_row else None
    before_paths: set[str] = set()
    if root_id is not None:
        before_paths = {
            str(row["rel_path"])
            for row in store.conn.execute(
                "SELECT rel_path FROM locations WHERE root_id=? AND missing_since IS NULL",
                (root_id,),
            )
        }

    audit = audit_root(root)
    targets = sync_state(store, cfg, root, audit, full_scan=True)

    after_paths = {str(t.rel_path) for t in targets}
    added = sorted(after_paths - before_paths)
    return {
        "on_disk": len(targets),
        "present_before": len(before_paths),
        "present_after": len(after_paths),
        "added": len(added),
        "added_paths": added,
        "missing": max(0, len(before_paths) - len(after_paths)),
    }


def run_apply(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    limit: int | None = None,
    only_rel_paths: set[str] | None = None,
    only_staged_ids: set[int] | None = None,
    force: bool = False,
    progress: ProgressFn = _noop,
    should_yield: Callable[[], bool] | None = None,
) -> RunSummary:
    """把暂存区里「已接受」的歌词写入音乐（备份 + 事务 + 复核）。

    `should_yield` 同 `run_fetch`：写好当前这一首就停下，把租约让给人工。

    `only_rel_paths` 用于单曲/选定范围写入：只处理这些相对路径对应的暂存行。

    `only_staged_ids` 用于「人工只写选中的那一条」：同一位置可能有多份
    待写入歌词（历史遗留），全写一遍不但白写，最终内容还取决于迭代顺序。

    `force` 表示人工明确选定：放宽纯文本歌词的复核要求（见 `WritePlan.force`）。
    """
    started = time.monotonic()
    summary = RunSummary()

    if not cfg.policy.write_enabled:
        summary.errors.append("write_enabled=false：全库只读，未执行任何写入")
        summary.elapsed_seconds = time.monotonic() - started
        return summary
    if root.read_only:
        summary.errors.append(f"受管根 {root.name} 标记为只读，未执行任何写入")
        summary.elapsed_seconds = time.monotonic() - started
        return summary

    backup_dir = cfg.storage.resolved_data_dir / "backups"
    base = root.resolved_path
    applied = 0
    written_paths: set[str] = set()

    # 逐文件隔离失败。为什么必须这样做：实际「一键写入」在约第 46 首撞上
    # SQLite 写锁，整个任务直接 failed，后面 70 多首全部没写——
    # **一个瞬时错误葬送了一整批**。某首失败只应影响那一首，其余照常推进。
    # **先把待写入行全部读出来，再开始写。**
    # `iter_staged()` 是游标：边写 `staged_lyrics`（改状态）边迭代同一张表，
    # 在同一连接上是不安全的——实际网页端触发的 apply 任务会卡在第一首不动
    # （同一份代码在独立进程里跑 1.2 秒就完成 3 首）。487 行一次性物化，
    # 内存代价可以忽略。
    rows = list(store.iter_staged())
    for row in rows:
        if limit is not None and applied >= limit:
            break
        if should_yield is not None and should_yield():
            summary.yielded = True
            break
        if only_rel_paths is not None and row["rel_path"] not in only_rel_paths:
            continue
        if only_staged_ids is not None and int(row["id"]) not in only_staged_ids:
            continue

        wrote = False
        for attempt in range(4):
            try:
                wrote = _apply_one(store, cfg, base, backup_dir, row, summary, force=force)
                break
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt < 3:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                # 重试用尽：**有意不把状态改成 failed**。
                # 标注 failed 会让这首永远不再重试；保持 staged 则下一轮
                # 批量写入会自动续跑——这正是「可恢复」的关键。
                summary.deferred_locked += 1
                summary.errors.append(
                    f"{row['rel_path']}: 数据库持续被占用，保留待写入状态，下轮自动重试"
                )
            except Exception as exc:  # noqa: BLE001 — 单文件故障不得终止整批
                summary.apply_failed += 1
                summary.errors.append(
                    f"{row['rel_path']}: {type(exc).__name__}: {exc}"
                )
                try:  # noqa: SIM105
                    store.set_staged_state(row["id"], "failed")
                except Exception:  # noqa: BLE001 — 连标记都失败也不能终止整批
                    pass  # noqa: SIM105
                break

        if wrote:
            applied += 1
            summary.applied += 1
            written_paths.add(row["rel_path"])
            # iter_staged 是生成器，拿不到总数；报总数会需要先物化整表，
            # 而写入本来就该边遍历边落盘。所以只报已完成数与已用时。
            # 前端已经显示任务类型（「写入」），这里不要再重复「已写入」。
            progress(f"{applied} 首 · {_human_seconds(time.monotonic() - started)}")

    # 写入成功后**必须刷新「观测」**。
    #
    # 界面（音乐库列表、问题分布）读的是 `lyric_observations` 里最新那一条，
    # 而写观测的只有 `sync_state`（审计/抓取路径）。写入路径原来不回写，
    # 于是「手动刮削并写入成功」之后文件里已经有歌词了，列表里还写着
    # 「无歌词」——刷新也没用，要等监控进程下一次周期对账才自愈。
    # 用户看到的正是这个：明明写成功了，刷新还是无歌词，于是怀疑写入失败。
    #
    # 只重扫本次真正写入的文件（不是整库），代价与写入条数成正比。
    if written_paths:
        try:
            refreshed = audit_root(root, only_rel_paths=written_paths)
            sync_state(store, cfg, root, refreshed)
        except Exception as exc:  # noqa: BLE001 — 文件已经写好了，
            # 刷新失败不能让调用方以为写入失败，如实报告即可。
            summary.errors.append(
                "写入已完成，但刷新界面状态失败（下次对账会自愈）："
                f"{type(exc).__name__}: {exc}"
            )

    # 写入成功 → 这一首已经有歌词了 → 队列里为它留着的候选**已经没有意义**。
    #
    # 为什么必须在这里做：`supersede_obsolete_reviews` 以前只有定义、
    # 从来没人调用，于是队列里越积越多「早就写好了、却还在等人决定」的条目
    # ——实际 58 首里有 67 条属于这种（按曲目算）。使用者看到的是
    # 「这 58 首还要我决定」，实际其中绝大多数已经不需要了。
    #
    # 放在观测刷新**之后**：判定依据是「此刻文件里有什么」，
    # 观测没刷新就清理会误判。
    if written_paths:
        try:
            store.supersede_obsolete_reviews()
        except Exception as exc:  # noqa: BLE001 — 清理失败不影响写入结果
            summary.errors.append(
                f"写入已完成，但清理过期复核记录失败：{type(exc).__name__}: {exc}"
            )

    summary.elapsed_seconds = time.monotonic() - started
    return summary
