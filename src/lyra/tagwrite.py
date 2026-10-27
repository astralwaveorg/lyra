"""标签写入：把「已勾选的字段」安全地落进音频文件。

## 与歌词写入的差别

歌词写入要动的是**内嵌歌词块或 sidecar**，最坏情况下整份文件都要备份。
标签写入不动音频数据：只替换 Vorbis comment 里的若干键，写入器会校验
「改写前后的压缩音频哈希逐字节一致」。因此这里不做整份文件备份，
只记下**这些字段在写入前是什么**——撤销时按字段还原即可。

## 安全边界

1. 只写调用方显式给出的字段；`same` 的字段不允许写。
2. 写入前重读文件：扫描之后文件被改过就拒绝，不拿旧提案覆盖新内容。
3. 一次失败只影响这一首，不中断整批。
4. 撤销前校验「当前值仍等于Lyra 写入的值」，否则拒绝——不能把
   写入之后别人的修改一起抹掉。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, RootConfig
from .db import StateStore
from .formats import read_all_tags
from .models import MediaFormat
from .tagsources import (
    ACTION_CONFLICT,
    ACTION_CORRECT,
    ACTION_FILL,
    ACTION_SAME,
    compare_fields,
)
from .writer import TagEditPlan, apply_tag_edit, audio_hash_for

__all__ = [
    "TagApplyOutcome",
    "apply_proposal",
    "undo_tag_write",
]


@dataclass(slots=True)
class TagApplyOutcome:
    ok: bool
    proposal_id: int = 0
    rel_path: str = ""
    applied: dict[str, list[str]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    audio_hash: str | None = None
    error: str | None = None
    remaining: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.removed)


def _resolve_target(root: RootConfig, rel_path: str) -> tuple[Path | None, str | None]:
    """受管根内的普通文件，拒绝符号链接与越界路径。"""
    base = root.resolved_path
    try:
        base_resolved = base.resolve(strict=True)
    except OSError as exc:
        return None, f"受管根不可用：{exc}"
    candidate = base / rel_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, "文件不存在"
    if not resolved.is_relative_to(base_resolved):
        return None, "路径越界"
    if candidate.is_symlink():
        return None, "拒绝写入符号链接"
    if not resolved.is_file():
        return None, "不是普通文件"
    return resolved, None


def _load_payload(row) -> dict:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _refresh_proposal(
    store: StateStore,
    row,
    payload: dict,
    current: Mapping[str, Sequence[str]],
    *,
    manual: bool = False,
) -> None:
    """写入后按磁盘真实内容重算剩余项。

    已全部满足：**人工写入**把它标成「已定案」（done）而不是删掉——
    人工决定必须留痕，否则下一轮扫描算出的匹配只要跟上次不同，这条曲子
    又会回到待审查队列，看起来像「人工确认没生效」。自动写入没有这层
    人工意图，照旧删行，避免表里堆几千条历史。

    只剩冲突就留在复核队列——不能因为「写成功」就把还没处理完的冲突一起藏起来。
    """
    candidate = payload.get("candidate") or {}
    rows = compare_fields(current, candidate)
    remaining = [
        r for r in rows if r["action"] in (ACTION_FILL, ACTION_CORRECT, ACTION_CONFLICT)
    ]
    payload = dict(payload)
    payload["rows"] = [
        {
            "key": r["key"],
            "current": list(r["current"]),
            "suggested": list(r["suggested"]),
            "action": r["action"],
        }
        for r in rows
    ]
    payload["current"] = {
        r["key"]: list(r["current"]) for r in rows if r["current"]
    }
    payload["summary"] = {
        "conflict": sum(1 for r in rows if r["action"] == ACTION_CONFLICT),
        "correct": sum(1 for r in rows if r["action"] == ACTION_CORRECT),
        "fill": sum(1 for r in rows if r["action"] == ACTION_FILL),
        "same": sum(1 for r in rows if r["action"] == ACTION_SAME),
        "total": len(rows),
    }
    if not remaining:
        if manual:
            # 行留着（人工痕迹），但状态改成「已定案」：队列里不再出现，
            # 扫描也不会再把它翻出来。对照内容一并刷新，界面看到的
            # 「剩余 0」才是真的。
            store.upsert_tag_proposal(
                location_id=int(row["location_id"]),
                state="done",
                source=str(row["source"]),
                confidence=float(row["confidence"] or 0),
                title=str(row["title"] or ""),
                artists=str(row["artists"] or ""),
                album=str(row["album"] or ""),
                release_id=str(row["release_id"] or ""),
                recording_id=str(row["recording_id"] or ""),
                duration_delta=row["duration_delta"],
                payload=payload,
                revive_skipped=True,
            )
        else:
            store.delete_tag_proposal(int(row["id"]))
        return
    has_conflict = any(r["action"] == ACTION_CONFLICT for r in remaining)
    # 链接指定是人工选定的来源：剩下的补空继续留在待审查，由人收尾，
    # 不自动挪进「推荐采纳」。
    state = "review" if (has_conflict or str(row["source"]) == "link") else "auto"
    store.upsert_tag_proposal(
        location_id=int(row["location_id"]),
        state=state,
        source=str(row["source"]),
        confidence=float(row["confidence"] or 0),
        title=str(row["title"] or ""),
        artists=str(row["artists"] or ""),
        album=str(row["album"] or ""),
        release_id=str(row["release_id"] or ""),
        recording_id=str(row["recording_id"] or ""),
        duration_delta=row["duration_delta"],
        payload=payload,
        revive_skipped=True,
    )


def apply_proposal(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    row,
    *,
    fields: Sequence[str] | None = None,
    require_auto: bool = False,
    manual: bool = True,
) -> TagApplyOutcome:
    """把提案里选中的字段写进文件。

    `fields=None` 表示「所有可补字段」——自动流程只走这一种，
    且 `require_auto=True` 时会再次确认没有冲突项混进来。

    `manual=True` 表示这是**人**点的写入。全部写完时提案会被标成「已定案」
    并留在库里（人工痕迹不丢，扫描不会再把它带回来）；自动写入
    （`manual=False`）照旧删行，表里不留历史。
    """
    proposal_id = int(row["id"])
    rel_path = str(row["rel_path"])
    outcome = TagApplyOutcome(ok=False, proposal_id=proposal_id, rel_path=rel_path)

    payload = _load_payload(row)
    rows = payload.get("rows") or []
    candidate = payload.get("candidate") or {}
    by_key = {
        str(item.get("key")): item for item in rows if isinstance(item, dict)
    }

    if fields is None:
        # 「补缺失」包含两类：本地空、以及按可信目录簇纠正发行级字段。
        chosen = [
            key for key, item in by_key.items()
            if item.get("action") in (ACTION_FILL, ACTION_CORRECT)
        ]
    else:
        chosen = []
        for raw in fields:
            key = str(raw).strip().upper()
            item = by_key.get(key)
            if item is None:
                outcome.error = f"字段 {key} 不在本次对比里"
                return outcome
            if item.get("action") == ACTION_SAME:
                continue
            chosen.append(key)

    if require_auto:
        if str(row["state"]) != "auto":
            outcome.error = "这条提案不是自动项"
            return outcome
        if any(by_key[key].get("action") == ACTION_CONFLICT for key in chosen):
            outcome.error = "自动写入不能覆盖冲突字段"
            return outcome

    wanted: dict[str, list[str]] = {}
    recorded: dict[str, list[str]] = {}
    for key in dict.fromkeys(chosen):
        values = [str(v) for v in (candidate.get(key) or []) if str(v).strip()]
        if not values:
            continue
        wanted[key] = values
        recorded[key] = [str(v) for v in (by_key[key].get("current") or [])]

    target, error = _resolve_target(root, rel_path)
    if target is None:
        outcome.error = error
        return outcome

    if not cfg.policy.write_enabled or root.read_only:
        outcome.error = "写入开关已关闭或该根只读"
        return outcome

    try:
        current, read_error = read_all_tags(target)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"读取标签失败：{type(exc).__name__}: {exc}"
        return outcome
    if read_error:
        outcome.error = read_error
        return outcome

    # 提案可能已经过期（扫描之后文件被别的工具改过）。逐字段重新核对：
    #   - 当前值已经等于建议值 → 不需要再写，视为满足；
    #   - 仍然为空 → 照常补；
    #   - 变成了另一个非空值 → 拒绝覆盖，并说清是哪个字段。
    # 原来的做法是「只要有任何字段与扫描时不同就整条失败」，于是文件里
    # 已有的 ARTISTS / 艺人 MBID 会让剩下的录音 MBID 永远写不进去。
    live_rows = {
        str(item.get("key")): item for item in compare_fields(current, candidate)
    }
    tags: dict[str, list[str]] = {}
    before: dict[str, list[str]] = {}
    for key, values in wanted.items():
        live = [str(v) for v in (current.get(key) or []) if str(v).strip()]
        same_now = str((live_rows.get(key) or {}).get("action") or "") == ACTION_SAME
        if live != recorded.get(key, []):
            if same_now:
                # 文件已经被别的工具写成了建议值，不必重复写
                continue
            if live:
                outcome.error = (
                    f"{key} 在扫描后已被修改为「{' / '.join(live)}」，"
                    "与提案不一致；请重新扫描后再写入"
                )
            else:
                outcome.error = (
                    f"{key} 在扫描后被清空，与提案不一致；请重新扫描后再写入"
                )
            return outcome
        tags[key] = values
        before[key] = live

    if not tags:
        # 没有可写字段（已经满足、或只剩冲突）：刷新提案，让界面不再显示
        # 那些其实已经写完的项；这不是错误。
        try:
            _refresh_proposal(store, row, payload, current, manual=manual)
            refreshed = store.tag_proposal(proposal_id)
            if refreshed is not None:
                outcome.remaining = len(
                    [
                        r for r in (_load_payload(refreshed).get("rows") or [])
                        if r.get("action")
                        in (ACTION_FILL, ACTION_CORRECT, ACTION_CONFLICT)
                    ]
                )
        except Exception:  # noqa: BLE001 — 刷新失败不影响「已满足」的结论
            pass
        outcome.ok = True
        outcome.error = None
        return outcome

    media_format = None
    try:
        from .formats import detect_media_format

        media_format, _handle, _err = detect_media_format(target)
    except Exception:  # noqa: BLE001
        media_format = None
    if media_format not in (
        MediaFormat.FLAC, MediaFormat.MP3, MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS
    ):
        outcome.error = (
            f"暂不支持写入 {getattr(media_format, 'value', '该格式')} 的标签"
        )
        return outcome

    outcome.audio_hash = audio_hash_for(target, media_format)
    result = apply_tag_edit(
        TagEditPlan(
            audio_path=target,
            media_format=media_format,
            tags=tags,
            remove=[],
            original_audio_hash=outcome.audio_hash or "",
        )
    )
    if not result.ok:
        outcome.error = result.error
        return outcome

    txn_id = f"tags-{uuid.uuid4().hex[:12]}"
    store.record_tag_write(
        txn_id=txn_id,
        location_id=int(row["location_id"]),
        before=before,
        applied=result.applied,
        audio_hash=result.audio_hash or None,
    )
    outcome.ok = True
    outcome.applied = dict(result.applied)
    outcome.removed = list(result.removed)
    outcome.audio_hash = result.audio_hash or outcome.audio_hash

    try:
        refreshed, _error = read_all_tags(target)
        _refresh_proposal(store, row, payload, refreshed, manual=manual)
        remaining_row = store.tag_proposal(proposal_id)
        if remaining_row is not None:
            remaining_payload = _load_payload(remaining_row)
            outcome.remaining = len(
                [
                    r for r in (remaining_payload.get("rows") or [])
                    if r.get("action") in (ACTION_FILL, ACTION_CORRECT, ACTION_CONFLICT)
                ]
            )
    except Exception:  # noqa: BLE001 — 刷新失败不影响写入结果
        pass
    return outcome


def undo_tag_write(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    row,
) -> TagApplyOutcome:
    """按字段还原一次标签写入。

    只还原**这次写入涉及的字段**，不碰其它标签。写入之后若有人又改了
    这些字段，当前值就会与 `applied` 不符，此时拒绝撤销。
    """
    outcome = TagApplyOutcome(ok=False, proposal_id=0, rel_path=str(row["rel_path"]))
    if row["undone_at"]:
        outcome.error = "这次写入已经撤销过"
        return outcome

    rel_path = str(row["rel_path"])
    target, error = _resolve_target(root, rel_path)
    if target is None:
        outcome.error = error
        return outcome
    if not cfg.policy.write_enabled or root.read_only:
        outcome.error = "写入开关已关闭或该根只读"
        return outcome

    try:
        before = json.loads(str(row["before_json"]))
        applied = json.loads(str(row["applied_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        outcome.error = "写入记录已损坏"
        return outcome
    if not isinstance(before, dict) or not isinstance(applied, dict) or not applied:
        outcome.error = "没有可撤销的字段"
        return outcome

    try:
        current, read_error = read_all_tags(target)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"读取标签失败：{type(exc).__name__}: {exc}"
        return outcome
    if read_error:
        outcome.error = read_error
        return outcome

    for key, values in applied.items():
        got = [str(v) for v in (current.get(key) or []) if str(v).strip()]
        if got != [str(v) for v in values]:
            outcome.error = f"{key} 在写入后又被修改，拒绝撤销以免覆盖新改动"
            return outcome

    restore: dict[str, list[str]] = {}
    remove: list[str] = []
    for key in applied:
        values = [str(v) for v in (before.get(key) or []) if str(v).strip()]
        if values:
            restore[key] = values
        else:
            remove.append(key)

    from .formats import detect_media_format

    media_format, _handle, _err = detect_media_format(target)
    if media_format not in (
        MediaFormat.FLAC, MediaFormat.MP3, MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS
    ):
        outcome.error = "暂不支持撤销该格式的标签写入"
        return outcome

    result = apply_tag_edit(
        TagEditPlan(
            audio_path=target,
            media_format=media_format,
            tags=restore,
            remove=remove,
            original_audio_hash=audio_hash_for(target, media_format) or "",
        )
    )
    if not result.ok:
        outcome.error = result.error
        return outcome

    store.mark_tag_write_undone(int(row["id"]))
    outcome.ok = True
    outcome.applied = restore
    outcome.removed = remove
    outcome.audio_hash = result.audio_hash
    return outcome
