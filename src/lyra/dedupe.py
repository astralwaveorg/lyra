"""重复音频检测：只呈现、不删除。

## 判定证据（从强到弱）

1. **压缩音频哈希相同**：去掉标签后的音频字节完全一致，几乎可以确定是同一份。
2. **MusicBrainz 录音 ID 相同**：同一录音的不同压制/不同容器。
3. **AcoustID 指纹结果里的录音 ID 相同**：本地缓存，不再联网。
4. **标题 + 艺人 + 时长相近**：兜底，标记为「相似」并要求人工确认。

第 4 类最弱：同名同艺人但时长差 2 秒以内仍可能是不同版本。
界面上会写明依据，勾选只是**建议**，真正删除必须人工确认。

## 不自动删除

音频哈希相同也可能是有意保留的两份（例如一份在车载目录）。
删除是不可逆的领域动作，必须由人看过并排对比后决定。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import scan
from .config import Config, RootConfig
from .db import StateStore
from .fileops import move_to_trash, trash_root
from .formats import read_audio_meta
from .models import MediaFormat
from .writer import audio_hash_for

__all__ = [
    "DedupeSummary",
    "scan_duplicates",
    "delete_duplicates",
    "quality_label",
]

_FORMAT_RANK = {
    MediaFormat.FLAC: 40,
    MediaFormat.OGG_OPUS: 30,
    MediaFormat.OGG_VORBIS: 25,
    MediaFormat.MP3: 20,
}


@dataclass(slots=True)
class DedupeSummary:
    scanned: int = 0
    groups: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "groups": self.groups,
            "duplicates": self.duplicates,
            "errors": self.errors[:20],
            "elapsed_seconds": round(self.elapsed, 2),
        }


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\s\-_.,;:!?/\\|·•~()\[\]{}<>]+", "", value)


def _tags(meta: Any) -> dict[str, list[str]]:
    return {str(k).upper(): list(v) for k, v in (meta.tags or {}).items()}


def _first(tags: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        for value in tags.get(key.upper(), []) or []:
            text = str(value).strip()
            if text:
                return text
    return ""


def _fingerprint_recording_ids(store: StateStore, location_id: int | None) -> list[str]:
    """从缓存的 AcoustID 结果里取录音 ID（不联网）。"""
    if location_id is None:
        return []
    row = store.conn.execute(
        "SELECT result_json FROM fingerprints WHERE location_id=?", (location_id,)
    ).fetchone()
    if row is None or not row["result_json"]:
        return []
    try:
        matches = json.loads(str(row["result_json"]))
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for item in matches if isinstance(matches, list) else []:
        if not isinstance(item, dict):
            continue
        for rid in item.get("recording_ids") or []:
            if rid and str(rid) not in out:
                out.append(str(rid))
    return out


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _quality(entry: dict[str, Any]) -> tuple[int, int, int, int]:
    """保留哪一份：格式优先，其次标签完整度、歌词、体积。"""
    rank = _FORMAT_RANK.get(MediaFormat(entry["format"]), 0) if entry["format"] else 0
    return (
        rank,
        int(entry.get("tag_count") or 0),
        1 if entry.get("has_lrc") else 0,
        int(entry.get("size") or 0),
    )


def quality_label(entry: dict[str, Any]) -> str:
    try:
        fmt = MediaFormat(entry["format"])
    except (ValueError, KeyError):
        fmt = MediaFormat.UNSUPPORTED
    if fmt is MediaFormat.FLAC:
        return "FLAC 无损，优先保留"
    if fmt is MediaFormat.OGG_OPUS:
        return "Opus 编码，优先保留"
    if fmt is MediaFormat.OGG_VORBIS:
        return "Vorbis 编码"
    if fmt is MediaFormat.MP3:
        return "MP3 编码"
    return "格式未知"


def scan_duplicates(
    store: StateStore,
    cfg: Config,
    roots: list[RootConfig],
    *,
    progress: Any = None,
    full: bool = True,
) -> dict[str, Any]:
    """扫描并重建重复组。只读音频文件，不修改任何用户文件。"""
    started = time.time()
    summary = DedupeSummary()
    all_groups_touched = 0

    for root in roots:
        base = root.resolved_path
        if not base.is_dir():
            continue
        root_id = store.get_root_id(root.name)
        if root_id is None:
            root_id = store.upsert_root(
                name=root.name, role=root.role.value, path=str(root.resolved_path),
                enabled=root.enabled, read_only=root.read_only,
            )
        entries: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        files = sorted(scan.iter_audio_files(base, root))
        for index, path in enumerate(files):
            if progress and index and index % 100 == 0:
                progress(f"{root.name}: 已分析 {index}/{len(files)} 个文件…")
            try:
                stat = path.stat()
                rel = str(path.relative_to(base))
            except OSError:
                continue
            seen_paths.add(rel)
            cached = store.cached_audio_index(
                root_id, rel, file_size=stat.st_size, file_mtime=stat.st_mtime_ns
            )
            if cached is not None:
                entries.append(
                    {
                        "rel_path": rel,
                        "size": int(cached["file_size"]),
                        "mtime": float(stat.st_mtime),
                        "format": str(cached["media_format"]),
                        "audio_hash": str(cached["audio_hash"]),
                        "duration": cached["duration"],
                        "title": str(cached["title"] or ""),
                        "artist": str(cached["artist"] or ""),
                        "album": str(cached["album"] or ""),
                        "recording_id": str(cached["recording_id"] or ""),
                        "has_lrc": (base / rel).with_suffix(".lrc").is_file(),
                        "tag_count": sum(
                            1 for key in ("title", "artist", "album")
                            if cached[key]
                        ),
                    }
                )
                continue

            try:
                meta = read_audio_meta(path, base)
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"{rel}: 读取失败 {type(exc).__name__}: {exc}")
                continue
            if not meta.info.media_format.supported:
                continue
            audio_hash = audio_hash_for(path, meta.info.media_format)
            if not audio_hash:
                summary.errors.append(f"{rel}: 无法计算音频哈希")
                continue
            tags = _tags(meta)
            title = _first(tags, "TITLE") or path.stem
            artist = _first(tags, "ARTIST", "ARTISTS")
            album = _first(tags, "ALBUM")
            recording_id = _first(tags, "MUSICBRAINZ_TRACKID")
            location = store.location_by_rel_path(root_id, rel)
            location_id = int(location["id"]) if location is not None else None
            fingerprint_ids = _fingerprint_recording_ids(store, location_id)
            if not recording_id and fingerprint_ids:
                recording_id = fingerprint_ids[0]
            duration = meta.info.duration
            store.upsert_audio_index(
                root_id=root_id,
                rel_path=rel,
                file_size=stat.st_size,
                file_mtime=stat.st_mtime_ns,
                media_format=meta.info.media_format.value,
                audio_hash=audio_hash,
                duration=duration,
                title=title,
                artist=artist,
                album=album,
                recording_id=recording_id,
            )
            entries.append(
                {
                    "rel_path": rel,
                    "size": int(stat.st_size),
                    "mtime": float(stat.st_mtime),
                    "format": meta.info.media_format.value,
                    "audio_hash": audio_hash,
                    "duration": duration,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "recording_id": recording_id,
                    "has_lrc": (base / rel).with_suffix(".lrc").is_file(),
                    "tag_count": sum(1 for value in (title, artist, album) if value),
                }
            )
        store.prune_audio_index(root_id, seen_paths)
        summary.scanned += len(entries)
        groups = _build_groups(entries)
        payloads = []
        for group in groups:
            indices = list(
                dict.fromkeys([*group["members"], *group["references"]])
            )
            member_entries = [entries[index] for index in indices]
            items = _group_items(base, member_entries)
            if len(items) < 2:
                continue
            reference_paths = {
                str(entries[index]["rel_path"]) for index in group["references"]
            }
            for item in items:
                item["reference"] = item["rel_path"] in reference_paths
            # 质量相同时优先保留已经属于另一个分类的参照项，避免同一份文件
            # 在一个组里是保留、在另一个组里又变成待删除。
            keep = max(
                items, key=lambda item: (_quality(item), bool(item.get("reference")))
            )
            for item in items:
                # 参照项属于另一个分类，绝不在这里删除。
                item["suggested_delete"] = (
                    not item["reference"] and item["rel_path"] != keep["rel_path"]
                )
                # 保留理由属于**保留的那一份**，不能挂在待删除的副本上。
                item["keep_reason"] = (
                    quality_label(keep) if item["rel_path"] == keep["rel_path"] else ""
                )
            payloads.append(
                {
                    "layout": 2,
                    "group_key": group["key"],
                    "kind": group["kind"],
                    "keep_rel_path": keep["rel_path"],
                    "keep_reason": quality_label(keep),
                    "items": items,
                }
            )
        store.replace_dedupe_groups(root_id, payloads)
        all_groups_touched += len(payloads)
        summary.duplicates += sum(
            1 for payload in payloads for item in payload["items"]
            if item["suggested_delete"]
        )

    summary.groups = all_groups_touched
    summary.elapsed = time.time() - started
    store.set_meta("dedupe.last_scan_at", str(time.time()))
    store.set_meta(
        "dedupe.last_scan_summary",
        json.dumps(summary.to_dict(), ensure_ascii=False),
    )
    return summary.to_dict()


def _best(entries: list[dict[str, Any]], indices: list[int]) -> int:
    return max(indices, key=lambda index: _quality(entries[index]))


def _group_key(
    kind: str, entries: list[dict[str, Any]], members: list[int], references: list[int]
) -> str:
    paths = sorted({str(entries[i]["rel_path"]) for i in [*members, *references]})
    return hashlib.sha256((kind + "|" + "|".join(paths)).encode()).hexdigest()[:24]


def _build_groups(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按证据强弱分组成**互不混淆**的三类。

    关键约束：一个文件的「可删除」身份只能属于一类。
      1. 音频哈希相同 → `exact`，组内全部是精确副本；
      2. 录音 ID 相同但哈希不同 → `recording`；只收尚未进入 exact 的文件，
         已属于 exact 组的那一份只作为**受保护的参照**（reference），
         不参与本组的删除；
      3. 标题 + 艺人 + 时长相近 → `similar`；同样只收尚未进入前两类的文件。

    这样按分类一键删除时，不会把另一个分类的文件一起带走。参照项在界面上
    明确标为「其他分类保留」，复选框禁用。
    """
    if not entries:
        return []

    by_hash: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        audio_hash = str(entry.get("audio_hash") or "")
        if audio_hash:
            by_hash.setdefault(audio_hash, []).append(index)

    groups: list[dict[str, Any]] = []
    assigned: dict[int, str] = {}

    # 1) 音频完全一致
    for members in by_hash.values():
        if len(members) < 2:
            continue
        groups.append(
            {
                "kind": "exact",
                "members": list(members),
                "references": [],
                "key": _group_key("exact", entries, members, []),
            }
        )
        for index in members:
            assigned[index] = "exact"

    # 2) 同一录音：精确副本组只提供参照，不再被本组删除
    by_recording: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        recording = str(entry.get("recording_id") or "")
        if recording:
            by_recording.setdefault(recording, []).append(index)

    for recording, members in by_recording.items():
        candidates = [index for index in members if index not in assigned]
        if not candidates:
            continue
        exact_matches = [
            index for index in members
            if assigned.get(index) == "exact"
            and str(entries[index].get("recording_id") or "") == recording
        ]
        references = [_best(entries, exact_matches)] if exact_matches else []
        if len(candidates) + len(references) < 2:
            continue
        groups.append(
            {
                "kind": "recording",
                "members": candidates,
                "references": references,
                "key": _group_key("recording", entries, candidates, references),
            }
        )
        for index in candidates:
            assigned[index] = "recording"

    # 3) 标题/艺人/时长相近。参考项只来自已分组的文件，同样受保护。
    buckets: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        title = _normalize(entry.get("title") or "")
        artist = _normalize(entry.get("artist") or "")
        if not title or not artist or entry.get("duration") is None:
            continue
        buckets.setdefault(f"{artist}|{title}", []).append(index)

    for indices in buckets.values():
        pending = [index for index in indices if index not in assigned]
        if not pending:
            continue
        pending.sort(key=lambda index: float(entries[index]["duration"]))
        clusters: list[list[int]] = []
        current = [pending[0]]
        for index in pending[1:]:
            # 以簇内第一个（最短）时长为锚，保证整个簇的跨度不超过 2 秒。
            # 若按「相邻差」串联，1/3/5 秒会被连成跨度 4 秒的伪同录音。
            if (
                float(entries[index]["duration"])
                - float(entries[current[0]]["duration"])
                <= 2.0
            ):
                current.append(index)
            else:
                clusters.append(current)
                current = [index]
        clusters.append(current)

        for cluster in clusters:
            candidates = [index for index in cluster if index not in assigned]
            if not candidates:
                continue
            reference: int | None = None
            for index in indices:
                # 相似组之间不再互相参照，否则 1/3/5 秒会通过传递关系
                # 被串成同一堆重复。参照只来自精确副本或同一录音组。
                if assigned.get(index) not in ("exact", "recording"):
                    continue
                duration = float(entries[index]["duration"])
                if any(
                    abs(duration - float(entries[item]["duration"])) <= 2.0
                    for item in candidates
                ) and (
                    reference is None
                    or _quality(entries[index]) > _quality(entries[reference])
                ):
                    reference = index
            references = [reference] if reference is not None else []
            if len(candidates) + len(references) < 2:
                continue
            groups.append(
                {
                    "kind": "similar",
                    "members": candidates,
                    "references": references,
                    "key": _group_key("similar", entries, candidates, references),
                }
            )
            for index in candidates:
                assigned[index] = "similar"

    return groups


def _group_items(base: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for entry in entries:
        path = base / entry["rel_path"]
        has_lrc = path.with_suffix(".lrc").is_file()
        items.append(
            {
                **entry,
                "name": path.name,
                "has_lrc": has_lrc,
                "tag_count": sum(
                    1
                    for key in ("title", "artist", "album")
                    if entry.get(key)
                ),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 人工确认后的删除
# ---------------------------------------------------------------------------


def _validate_selection(
    row: Any, rel_paths: list[str]
) -> tuple[list[str], list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    try:
        items = json.loads(str(row["items_json"]))
    except (TypeError, ValueError):
        return [], [], {}, "重复组数据已损坏，请重新扫描"
    if any(isinstance(item, dict) and "reference" not in item for item in items):
        return [], [], {}, "该重复组来自旧分类规则，请重新检测后再处理"
    by_path = {
        str(item.get("rel_path")): item
        for item in items
        if isinstance(item, dict)
    }
    selected: list[str] = []
    for rel in dict.fromkeys(rel_paths):
        item = by_path.get(rel)
        if item is None:
            return [], [], by_path, f"{rel} 不属于这个重复组"
        if item.get("reference"):
            return [], [], by_path, f"{rel} 属于其他分类，不能在这里删除"
        # 人工改选时允许删掉应用原本建议保留的那一份：判断错了要能改。
        # 参照项永远不参与删除，每组至少留一份由下面的检查兜底。
        selected.append(rel)
    if not selected:
        return [], [], by_path, "没有选中可删除的副本"
    if len(selected) >= len(by_path):
        return [], [], by_path, "至少要保留一份，不能把整组都删除"
    return selected, items, by_path, None


def _delete_one_root(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    selections: dict[int, list[str]],
) -> dict[str, Any]:
    """在单个受管根内执行一批删除；先整体校验，再动文件。"""
    validated: list[tuple[Any, list[str], list[dict[str, Any]]]] = []
    for group_id, rel_paths in selections.items():
        row = store.dedupe_group(int(group_id))
        if row is None:
            return {"ok": False, "deleted": 0, "errors": [f"重复组 {group_id} 不存在"], "batch_ids": []}
        if str(row["root_name"]) != root.name:
            return {"ok": False, "deleted": 0, "errors": ["重复组不属于当前曲库"], "batch_ids": []}
        selected, items, _by_path, error = _validate_selection(row, rel_paths)
        if error:
            return {"ok": False, "deleted": 0, "errors": [error], "batch_ids": []}
        validated.append((row, selected, items))

    batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    base = root.resolved_path
    deleted = 0
    errors: list[str] = []
    deleted_paths: set[str] = set()
    for row, selected, items in validated:
        removed: set[str] = set()
        for rel in selected:
            path = base / rel
            if not path.is_file():
                errors.append(f"{rel}: 文件已不存在")
                continue
            try:
                trash_rel = move_to_trash(cfg, root, path, batch_id)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
                continue
            detail: dict[str, Any] = {}
            sidecar = path.with_suffix(".lrc")
            if sidecar.is_file():
                try:
                    detail["sidecar"] = move_to_trash(cfg, root, sidecar, batch_id)
                except OSError as exc:
                    errors.append(f"{rel}: 歌词移动失败 {exc}")
            store.record_organize_op(
                root_id=int(row["root_id"]),
                batch_id=batch_id,
                op_kind="delete",
                old_rel_path=rel,
                trash_rel_path=trash_rel,
                detail=json.dumps(detail, ensure_ascii=False),
            )
            removed.add(rel)
            deleted_paths.add(rel)
            deleted += 1

        remaining = [item for item in items if str(item.get("rel_path")) not in removed]
        candidates = [item for item in remaining if not item.get("reference")]
        if len(remaining) < 2 or not candidates:
            store.dismiss_dedupe_group(int(row["id"]))
            continue
        keep = max(
            remaining,
            key=lambda item: (_quality(item), bool(item.get("reference"))),
        )
        for item in remaining:
            item["suggested_delete"] = (
                not item.get("reference")
                and str(item.get("rel_path")) != str(keep.get("rel_path"))
            )
            item["keep_reason"] = (
                quality_label(keep)
                if str(item.get("rel_path")) == str(keep.get("rel_path"))
                else ""
            )
        store.update_dedupe_group(
            int(row["id"]),
            keep_rel_path=str(keep.get("rel_path")),
            keep_reason=quality_label(keep),
            items=remaining,
        )

    # 删除后按磁盘现状重建这个根的分类。否则被删掉的参照项会留在其它组，
    # 或者精确副本组消失后、同一录音组无法补上新的参照。
    # 全量哈希有缓存，正常情况下这只是秒级操作。
    with contextlib.suppress(Exception):
        # 重建失败不影响已经完成的删除
        scan_duplicates(store, cfg, [root])

    return {
        "ok": not errors,
        "deleted": deleted,
        "errors": errors,
        "batch_id": batch_id,
        "batch_ids": [batch_id],
        "trash_dir": str(trash_root(cfg) / batch_id),
    }


def delete_duplicates_selected(
    store: StateStore, cfg: Config, selections: dict[int, list[str]]
) -> dict[str, Any]:
    """按组删除选中的副本；所有组先校验，再按受管根分批执行。

    `selections` 是 `{group_id: [rel_path, ...]}`。参照项（属于其它分类）
    永远不能在这里删除；人工改选时允许删掉应用原本建议保留的那一份，
    但每组至少要留一份，所以按分类一键处理不会带走别的分类。
    """
    if not selections:
        return {"ok": False, "deleted": 0, "errors": ["没有选中任何副本"], "batch_ids": []}

    by_root: dict[str, dict[int, list[str]]] = {}
    for group_id, rel_paths in selections.items():
        row = store.dedupe_group(int(group_id))
        if row is None:
            return {"ok": False, "deleted": 0, "errors": [f"重复组 {group_id} 不存在"], "batch_ids": []}
        if not any(item.name == str(row["root_name"]) for item in cfg.roots):
            return {
                "ok": False, "deleted": 0,
                "errors": [f"受管根 {row['root_name']} 当前未配置"],
                "batch_ids": [],
            }
        _selected, _items, _by_path, error = _validate_selection(row, rel_paths)
        if error:
            return {"ok": False, "deleted": 0, "errors": [error], "batch_ids": []}
        by_root.setdefault(str(row["root_name"]), {})[int(group_id)] = rel_paths

    total_deleted = 0
    all_errors: list[str] = []
    batch_ids: list[str] = []
    for root_name, root_selections in by_root.items():
        root = next((item for item in cfg.roots if item.name == root_name), None)
        if root is None:
            return {
                "ok": False, "deleted": total_deleted,
                "errors": [*all_errors, f"受管根 {root_name} 当前未配置"],
                "batch_ids": batch_ids,
            }
        result = _delete_one_root(store, cfg, root, root_selections)
        total_deleted += int(result.get("deleted") or 0)
        all_errors.extend(result.get("errors") or [])
        batch_ids.extend(result.get("batch_ids") or [])
        if (
            not result.get("ok")
            and result.get("deleted") == 0
            and any(
                marker in error
                for error in result.get("errors") or []
                for marker in ("不属于", "其他分类", "至少要保留", "旧分类规则")
            )
        ):
            # 校验阶段失败：不要再动其它根
            return {
                "ok": False, "deleted": total_deleted,
                "errors": [*all_errors], "batch_ids": batch_ids,
            }
    return {
        "ok": not all_errors,
        "deleted": total_deleted,
        "errors": all_errors,
        "batch_ids": batch_ids,
        "trash_dirs": [str(trash_root(cfg) / batch) for batch in batch_ids],
    }


def _decode_items(row: Any) -> list[dict[str, Any]]:
    """解码重复组的 items_json。"""
    try:
        data = json.loads(str(row["items_json"] or "[]"))
    except (TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)]


#: 自动清理的判定：四项**全同**才算「完全一样」。
#:
#: - 音频哈希相同（压缩后的音频数据逐字节一致——同一首歌的两次转码
#:   会得到不同哈希，所以这条最严）；
#: - 时长相同（哈希之外的独立复核，防哈希实现有洞）；
#: - 标题、艺人、专辑三者规范化后相同（简繁、大小写、全半角、空格
#:   差异不算不同——那正是「同一首的不同写法」）；
#: - 专辑名相同才允许自动删：**不同专辑的同名曲不是重复**
#:   （`The Beatles/1/24 - Something.flac` 与 `Abbey Road/Something.flac`
#:   是同录音不同发行，删掉会丢内容）。
_AUTO_CLEAN_FIELDS = ("title", "artist", "album")


def _auto_same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两份音频是否「完全一样」（可自动清理的前提）。

    严判的理由：错删一首曲子比多留一个待办糟得多。宁可漏过「同曲的
    两次转码」（哈希不同）也不误删「同曲不同专辑」（专辑名不同）。
    """
    if str(a.get("audio_hash") or "") != str(b.get("audio_hash") or ""):
        return False
    if not str(a.get("audio_hash") or ""):
        return False
    da, db = a.get("duration"), b.get("duration")
    if da is None or db is None or abs(float(da) - float(db)) > 0.01:
        return False
    for key in _AUTO_CLEAN_FIELDS:
        if _normalize(a.get(key) or "") != _normalize(b.get(key) or ""):
            return False
    return True


def clean_exact_duplicates(
    store: StateStore,
    cfg: Config,
    roots: list[RootConfig],
    *,
    dry_run: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """自动清理「完全一样」的重复文件：保留最优的一份，其余进回收站。

    只处理**判定为完全一样**的组（同音频哈希 + 同时长 + 同标题/艺人/专辑）。
    「同一录音但不同专辑」「同曲不同版本」「仅标题相近」这些一律不动，
    仍留在重复检测面板由人判断。

    删除走回收站批次，可撤销（与整理页的删除语义一致）。
    """
    started = time.time()
    result: dict[str, Any] = {
        "ok": True, "scanned_groups": 0, "groups": 0, "deleted": 0,
        "kept": 0, "errors": [], "batch_ids": [], "dry_run": bool(dry_run),
    }
    for root in roots:
        base = root.resolved_path
        if not base.is_dir():
            continue
        root_id = store.get_root_id(root.name)
        if root_id is None:
            continue
        for row in store.iter_dedupe_groups(include_dismissed=False):
            if int(row["root_id"]) != int(root_id):
                continue
            result["scanned_groups"] += 1
            items = _decode_items(row)
            if len(items) < 2:
                continue
            # 只在「同哈希」的组里找完全一样的对
            by_hash: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                by_hash.setdefault(str(item.get("audio_hash") or ""), []).append(item)
            for same_hash in by_hash.values():
                if len(same_hash) < 2:
                    continue
                keep = _best(same_hash, list(range(len(same_hash))))
                keeper = same_hash[keep]
                for item in same_hash:
                    if item is keeper:
                        continue
                    if not _auto_same(keeper, item):
                        continue
                    if item.get("reference"):
                        # 参照项属于其它分类，受保护
                        continue
                    rel = str(item.get("rel_path") or "")
                    if not rel:
                        continue
                    result["groups"] += 1
                    result["kept"] += 1
                    if dry_run:
                        continue
                    outcome = _auto_delete_one(store, cfg, root, rel)
                    if outcome.get("ok"):
                        result["deleted"] += 1
                        for batch in outcome.get("batch_ids") or []:
                            if batch not in result["batch_ids"]:
                                result["batch_ids"].append(batch)
                    else:
                        for message in outcome.get("errors") or []:
                            result["errors"].append(str(message))
                        result["ok"] = False
        if progress:
            progress(f"{root.name}: 已检查 {result['scanned_groups']} 组")
    result["elapsed_seconds"] = time.time() - started
    return result


def _auto_delete_one(
    store: StateStore, cfg: Config, root: RootConfig, rel: str
) -> dict[str, Any]:
    """把一个「完全一样」的副本移进回收站（可撤销）。"""
    base = root.resolved_path
    path = base / rel
    if not path.is_file():
        return {"ok": False, "errors": [f"{rel}: 文件已不存在"]}
    batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    try:
        trash_rel = move_to_trash(cfg, root, path, batch_id)
    except OSError as exc:
        return {"ok": False, "errors": [f"{rel}: {exc}"]}
    detail: dict[str, Any] = {}
    sidecar = path.with_suffix(".lrc")
    if sidecar.is_file():
        try:
            detail["sidecar"] = move_to_trash(cfg, root, sidecar, batch_id)
        except OSError as exc:
            detail["sidecar_error"] = str(exc)
    root_id = store.get_root_id(root.name)
    if root_id is not None:
        store.record_organize_op(
            root_id=int(root_id), batch_id=batch_id, op_kind="delete",
            old_rel_path=rel, trash_rel_path=trash_rel,
            detail=json.dumps(
                {**detail, "reason": "自动清理完全相同的重复文件"},
                ensure_ascii=False,
            ),
        )
    return {"ok": True, "deleted": 1, "batch_ids": [batch_id]}


def delete_duplicates(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    group_row: Any,
    rel_paths: list[str],
) -> dict[str, Any]:
    """兼容单组删除入口。"""
    result = _delete_one_root(
        store, cfg, root, {int(group_row["id"]): rel_paths}
    )
    if result.get("batch_ids"):
        result["batch_id"] = result["batch_ids"][0]
    return result
