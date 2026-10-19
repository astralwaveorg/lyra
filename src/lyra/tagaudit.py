"""批量标签审计：扫全库，统计 MusicBrainz 能补什么、冲突在哪。

## 为什么先做「只读审计」而不是直接写入

标签写入的影响面比歌词大——改错一个字段，整张专辑的信息都错。
而且「有多少可补、多少冲突」这个数字本身就是决策依据：
如果冲突只有几处，人工过一遍就完了；如果有几百处，说明匹配策略有问题，
这时候闷头写入只会制造一堆错误。

所以先出一份**可核对的账**，再决定是否落盘。

## 为什么以 MBID 为主路径

曲库 97% 的文件被 Picard 写过 `MUSICBRAINZ_ALBUMID`。按它精确查询
的结果唯一、无歧义、**零假冲突**（实际同一首歌按 MBID 查是 11 字段全一致，
而重新搜索会报出 5 个假冲突）。搜索只在缺 MBID 时兜底。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .audit import audit_root
from .config import Config, RootConfig
from .formats import read_all_tags, read_audio_meta
from .match import build_context
from .tagsources import (
    ACTION_CONFLICT,
    ACTION_FILL,
    MusicBrainzProvider,
    compare_fields,
    lookup_by_release,
)

__all__ = ["TagAuditSummary", "TagAuditItem", "audit_tags"]


@dataclass(slots=True)
class TagAuditItem:
    """单个文件的审计结果。"""

    rel_path: str
    title: str = ""
    artists: tuple[str, ...] = ()
    source_kind: str = ""
    """`mbid` = 按 MBID 精查；`search` = 标题艺人搜索；`none` = 没查到。"""

    album: str = ""
    duration_delta: float | None = None
    fill: dict[str, list[str]] = field(default_factory=dict)
    conflict: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    same: int = 0
    error: str | None = None

    @property
    def worth_applying(self) -> bool:
        return bool(self.fill)


@dataclass(slots=True)
class TagAuditSummary:
    scanned: int = 0
    with_mbid: int = 0
    resolved_exact: int = 0
    resolved_search: int = 0
    unresolved: int = 0
    tracks_with_fill: int = 0
    field_fill_counts: dict[str, int] = field(default_factory=dict)
    tracks_with_conflict: int = 0
    field_conflict_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    items: list[TagAuditItem] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "with_mbid": self.with_mbid,
            "resolved_exact": self.resolved_exact,
            "resolved_search": self.resolved_search,
            "unresolved": self.unresolved,
            "tracks_with_fill": self.tracks_with_fill,
            "field_fill_counts": dict(
                sorted(self.field_fill_counts.items(), key=lambda kv: -kv[1])
            ),
            "tracks_with_conflict": self.tracks_with_conflict,
            "field_conflict_counts": dict(
                sorted(self.field_conflict_counts.items(), key=lambda kv: -kv[1])
            ),
            "errors": self.errors[:20],
            "elapsed_seconds": self.elapsed_seconds,
            "items": [
                {
                    "rel_path": i.rel_path,
                    "title": i.title,
                    "artists": list(i.artists),
                    "album": i.album,
                    "source_kind": i.source_kind,
                    "duration_delta": i.duration_delta,
                    "fill": {k: list(v) for k, v in i.fill.items()},
                    "conflict": {k: [list(a), list(b)] for k, (a, b) in i.conflict.items()},
                    "same": i.same,
                    "error": i.error,
                }
                for i in self.items
            ],
        }


def audit_tags(
    cfg: Config,
    root: RootConfig,
    provider: MusicBrainzProvider,
    *,
    progress: Callable[[str], None] = lambda _m: None,
    limit: int | None = None,
    only_rel_paths: set[str] | None = None,
) -> TagAuditSummary:
    """扫描受管根，逐曲向 MusicBrainz 取建议并与当前标签对比。**只读。**"""
    started = time.monotonic()
    summary = TagAuditSummary()

    base = root.resolved_path
    audit = audit_root(root, only_rel_paths=only_rel_paths)
    tracks = [t for t in audit.tracks]
    if only_rel_paths is not None:
        tracks = [t for t in tracks if t.audio.rel_path in only_rel_paths]
    if limit is not None:
        tracks = tracks[:limit]

    for index, track in enumerate(tracks, start=1):
        summary.scanned += 1
        if index % 10 == 0:
            progress(f"已审计 {index}/{len(tracks)} 首…")

        path = base / track.audio.rel_path
        try:
            meta = read_audio_meta(path, base)
            all_tags, tags_error = read_all_tags(path)
            if tags_error:
                summary.errors.append(f"{track.audio.rel_path}: {tags_error}")
                continue
        except Exception as exc:  # noqa: BLE001 — 单文件故障不得中断整批
            summary.errors.append(f"{track.audio.rel_path}: {type(exc).__name__}: {exc}")
            continue

        album_id = str((all_tags.get("MUSICBRAINZ_ALBUMID") or [""])[0]).strip()
        has_mbid = bool(album_id)
        if has_mbid:
            summary.with_mbid += 1

        context = build_context(meta.tags, meta.info.duration)
        candidate = None
        kind = "none"
        try:
            if has_mbid:
                candidate = lookup_by_release(
                    provider,
                    release_id=album_id,
                    recording_id=str((all_tags.get("MUSICBRAINZ_TRACKID") or [""])[0]),
                    release_track_id=str(
                        (all_tags.get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
                    ),
                )
                if candidate is not None:
                    kind = "mbid"
            if candidate is None:
                found = provider.lookup(
                    title=context.title,
                    artists=context.artists,
                    release=context.album,
                    duration=context.duration,
                )
                if found:
                    candidate = found[0]
                    kind = "search"
        except Exception as exc:  # noqa: BLE001 — 单个来源故障不该中断整批
            summary.errors.append(f"{track.audio.rel_path}: MusicBrainz 查询失败 {exc}")
            continue

        if candidate is None:
            summary.unresolved += 1
            summary.items.append(
                TagAuditItem(rel_path=track.audio.rel_path, title=context.title,
                             artists=context.artists, source_kind="none")
            )
            continue

        if kind == "mbid":
            summary.resolved_exact += 1
        else:
            summary.resolved_search += 1

        rows = compare_fields(all_tags, candidate.fields)
        item = TagAuditItem(
            rel_path=track.audio.rel_path,
            title=context.title,
            artists=context.artists,
            album=candidate.album,
            source_kind=kind,
            duration_delta=(
                abs(candidate.duration - context.duration)
                if candidate.duration is not None and context.duration is not None
                else None
            ),
        )
        for row in rows:
            action = row["action"]
            if action == ACTION_FILL:
                item.fill[row["key"]] = list(row["suggested"])
                summary.field_fill_counts[row["key"]] = (
                    summary.field_fill_counts.get(row["key"], 0) + 1
                )
            elif action == ACTION_CONFLICT:
                item.conflict[row["key"]] = (list(row["current"]), list(row["suggested"]))
                summary.field_conflict_counts[row["key"]] = (
                    summary.field_conflict_counts.get(row["key"], 0) + 1
                )
            else:
                item.same += 1

        if item.fill:
            summary.tracks_with_fill += 1
        if item.conflict:
            summary.tracks_with_conflict += 1
        summary.items.append(item)

    progress(f"审计完成：扫描 {summary.scanned} 首")
    summary.elapsed_seconds = time.monotonic() - started
    return summary
