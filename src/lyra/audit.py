"""库审计：逐曲分类 + 库级健康检查。

这是「只读、绝不写音乐」的安全入口。

审计回答两个问题：
1. 每首曲目的歌词处于什么状态？（内嵌 / sidecar / 两者）
2. 库里有哪些结构性问题？（空 lrc、孤儿 lrc、重复 lrc、容器不符、缺封面…）
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import scan
from .config import RootConfig
from .formats import read_audio_meta
from .lrc import content_hash, decode_lyrics_bytes, normalize_lrc, parse_lrc
from .models import (
    AuditSummary,
    EmbeddedLyricsFinding,
    IssueCode,
    LyricsState,
    MediaFormat,
    SidecarFinding,
    TrackAudit,
)

__all__ = ["AuditResult", "audit_root", "render_audit_text", "pair_sidecars", "COVER_FILENAMES"]

#: 专辑目录中常见的封面文件名（大小写不敏感）
COVER_FILENAMES: frozenset[str] = frozenset(
    {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg",
     "folder.png", "album.jpg", "album.png", "front.jpg", "front.png"}
)


def pair_sidecars(
    audio_paths: Sequence[Path],
    sidecar_findings: Mapping[Path, SidecarFinding],
) -> dict[Path, tuple[Path | None, str]]:
    """把音频与同名/异名 sidecar 配对，返回 `{音频: (lrc 路径或 None, 方式)}`。

    为什么需要分级配对（要求「不自动猜测归属」，
    但完全不做又会产生重复歌词文件）：

    实际曲库存在两种命名不一致：
      - `01 漂向北方.flac` ↔ `1. 漂向北方.lrc`      （编号写法不同）
      - `09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac` ↔ `让你知道.lrc`（仅用曲名）

    若不做配对，这些 lrc 会被误报为孤儿，而 Lyra 随后会为同一首歌**再建一个**
    同名 lrc，结果是同一首歌两份歌词。

    分级原则是**只有唯一解才配对**，任何有歧义的情况都留给人：

    1. `exact`      —— 同名，最可靠
    2. `normalized` —— 去掉「碟号-音轨号」前缀后主体相同（编号写法差异）
    3. `contained`  —— lrc 主体是本音频主体的子串，且**双向唯一**：
                      该 lrc 只被这一个音频包含，该音频也只被这一个 lrc 包含。
                       双向唯一是这里的关键防御——只要存在第二个可能归属，就放弃配对。
    4. `ambiguous`  —— 有多个候选，明确标注而不是任选一个
    5. `none`       —— 没有候选
    """
    by_dir: dict[Path, list[Path]] = {}
    for path in sidecar_findings:
        by_dir.setdefault(path.parent, []).append(path)
    for pool in by_dir.values():
        pool.sort()

    def norm(path: Path) -> str:
        return scan.normalize_stem(path.stem)

    result: dict[Path, tuple[Path | None, str]] = {}
    used: set[Path] = set()

    # 一级：精确同名
    for audio in audio_paths:
        exact = scan.sidecar_path_for(audio)
        if exact in sidecar_findings:
            result[audio] = (exact, "exact")
            used.add(exact)

    def unmatched_pool(audio: Path) -> list[Path]:
        return [p for p in by_dir.get(audio.parent, []) if p not in used]

    # 二级：规范化主体唯一匹配
    for audio in audio_paths:
        if audio in result:
            continue
        target = norm(audio)
        candidates = [p for p in unmatched_pool(audio) if norm(p) == target]
        if len(candidates) == 1:
            result[audio] = (candidates[0], "normalized")
            used.add(candidates[0])

    # 三级：唯一包含匹配（双向唯一，**两个方向都算**）
    #
    #   A) 侧车主体 ⊆ 音频主体：`让你知道.lrc` ↔ `09 … - 让你知道.flac`
    #   B) 音频主体 ⊆ 侧车主体：`01 - 张国荣 - 童年时.lrc` ↔ `01 童年時.flac`
    #
    # 只做 A 方向是不够的：从网上下来的歌词常带「编号 - 艺人 - 曲名」的
    # 完整命名，比曲库里的文件名**更长**。实际整张《Salute》9 份 lrc
    # 因为方向不对而被报成孤儿，进而在界面上变成「找不到归属」。
    # B 方向同样要求「双向唯一」：该侧车只能被这一个音频包含，该音频也只能
    # 被这一个侧车包含（否则宁可报歧义，让人来定）。
    for audio in audio_paths:
        if audio in result:
            continue
        target = norm(audio)
        if len(target) < 2:
            continue
        hits: list[tuple[Path, str]] = []
        for candidate in unmatched_pool(audio):
            needle = norm(candidate)
            if len(needle) < 2:
                continue
            if needle in target:
                hits.append((candidate, "a"))
            elif target in needle:
                hits.append((candidate, "b"))
        if len(hits) != 1:
            continue
        candidate, direction = hits[0]
        if direction == "a":
            needle = norm(candidate)
            rivals = [
                other
                for other in audio_paths
                if other is not audio
                and other.parent == audio.parent
                and other not in result
                and needle in norm(other)
            ]
        else:
            needles = norm(candidate)
            rivals = [
                other
                for other in audio_paths
                if other is not audio
                and other.parent == audio.parent
                and other not in result
                and norm(other) in needles
            ]
        if not rivals:
            result[audio] = (candidate, "contained")
            used.add(candidate)

    # 标注歧义：有多个候选时绝不猜。
    # 这里必须同时考虑二级与三级两套候选，且**不能**套用三级的
    # 「主体至少 2 个字符」防抖——否则像 `1. D.lrc` / `2. D.lrc`
    # （主体都是 "d"）这种真实歧义会被误判成「没有候选」而静默放行。
    #
    # 歧义有两种形态，都要报：
    #   a) 一个音频面对多个候选
    #   b) 一个候选被多个音频争夺（三级配对因「双向唯一」而放弃的正是这种）
    # 只报 a) 会让 b) 静默显示为「没有候选」，用户看不到需要人工处理的项。
    for audio in audio_paths:
        if audio in result:
            continue
        target = norm(audio)
        pool = unmatched_pool(audio)
        exact_norm = [p for p in pool if norm(p) == target]
        contained = [
            p for p in pool
            if len(norm(p)) >= 2 and (norm(p) in target or target in norm(p))
        ]
        hits = {*exact_norm, *contained}
        ambiguous = len(hits) > 1

        if not ambiguous and len(contained) == 1:
            needle = norm(contained[0])
            contested = [
                other
                for other in audio_paths
                if other is not audio
                and other.parent == audio.parent
                and other not in result
                and needle in norm(other)
            ]
            ambiguous = bool(contested)

        result[audio] = (None, "ambiguous" if ambiguous else "none")

    return result


@dataclass(slots=True)
class AuditResult:
    root_name: str = ""
    root_path: str = ""
    summary: AuditSummary = field(default_factory=AuditSummary)
    tracks: list[TrackAudit] = field(default_factory=list)
    orphan_sidecars: list[str] = field(default_factory=list)
    duplicate_sidecars: list[tuple[str, list[str]]] = field(default_factory=list)
    empty_sidecars: list[str] = field(default_factory=list)
    bom_sidecars: list[str] = field(default_factory=list)
    sidecar_states: dict[str, int] = field(default_factory=dict)
    """全部 .lrc 文件按解析状态计数（含孤儿，因此与「已配对的 sidecar」不同）。"""

    unreadable: list[tuple[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def needs_work(self) -> list[TrackAudit]:
        """需要写入歌词的曲目（按 skip_if_valid_synced 的语义）。"""
        return [
            t
            for t in self.tracks
            if t.audio.media_format.supported
            and t.audio.parse_error is None
            and t.effective_state is not LyricsState.SYNCED
        ]


def audit_root(
    root: RootConfig,
    *,
    progress: object | None = None,
    only_rel_paths: set[str] | None = None,
) -> AuditResult:
    """审计一个受管根。只读，不修改任何文件。

    `only_rel_paths` 用于增量场景（监听事件触发的批次）：只读取这些相对路径，
    避免每次事件都全库重扫。为 None 时审计全根。
    """
    started = time.monotonic()
    base = root.resolved_path
    result = AuditResult(root_name=root.name, root_path=str(base))

    if not base.is_dir():
        result.unreadable.append((str(base), "受管根不存在或不是目录"))
        return result

    incremental = only_rel_paths is not None

    def _selected(path: Path) -> bool:
        if not incremental:
            return True
        try:
            return str(path.relative_to(base)) in only_rel_paths  # type: ignore[operator]
        except ValueError:
            return False

    # ---- 1) 目录中是否有图片封面 ----
    # 增量模式下只需看被选中文件所在目录，不必遍历全根
    dir_has_image: dict[Path, bool] = {}
    if incremental:
        for rel in only_rel_paths or ():
            parent = (base / rel).parent
            if parent in dir_has_image:
                continue
            try:
                with os.scandir(parent) as it:
                    dir_has_image[parent] = any(
                        Path(e.name).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                        for e in it
                    )
            except OSError:
                dir_has_image[parent] = False
    else:
        for path in _iter_all_files(base, root):
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                dir_has_image[path.parent] = True

    # ---- 2) sidecar 清单 ----
    # 增量模式仍需要「被选中文件所在目录」的全部 sidecar，
    # 否则配对与孤儿判定会失真。
    sidecar_findings: dict[Path, SidecarFinding] = {}
    sidecars_by_dir: dict[Path, list[Path]] = {}
    sidecar_states: dict[str, int] = {}
    selected_dirs: set[Path] = set()
    if incremental:
        for rel in only_rel_paths or ():
            selected_dirs.add((base / rel).parent)
    for lrc_path in scan.iter_sidecar_files(base, root):
        if incremental and lrc_path.parent not in selected_dirs:
            continue
        finding = _read_sidecar(lrc_path)
        sidecar_findings[lrc_path] = finding
        sidecars_by_dir.setdefault(lrc_path.parent, []).append(lrc_path)
        sidecar_states[finding.state.value] = sidecar_states.get(finding.state.value, 0) + 1
        if finding.has_bom:
            result.bom_sidecars.append(_rel(lrc_path, base))
        if finding.size == 0:
            result.empty_sidecars.append(_rel(lrc_path, base))

    # ---- 3) 逐曲审计 ----
    audio_paths = sorted(p for p in scan.iter_audio_files(base, root) if _selected(p))
    pairing = pair_sidecars(audio_paths, sidecar_findings)
    consumed_sidecars: set[Path] = set()
    by_format: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_issue: dict[str, int] = {}

    for audio_path in audio_paths:
        try:
            meta = read_audio_meta(audio_path, base)
        except Exception as exc:  # 极端情况：stat 失败等
            result.unreadable.append((_rel(audio_path, base), f"{type(exc).__name__}: {exc}"))
            continue

        info = meta.info
        by_format[info.media_format.value] = by_format.get(info.media_format.value, 0) + 1

        sidecar_path, pairing_note = pairing.get(audio_path, (None, "none"))
        sidecar = (
            sidecar_findings.get(sidecar_path) if sidecar_path is not None else None
        ) or SidecarFinding()
        if sidecar.exists:
            consumed_sidecars.add(sidecar_path)

        issues: list[IssueCode] = []
        if pairing_note in ("normalized", "contained"):
            issues.append(IssueCode.SIDECAR_NAME_MISMATCH)
        elif pairing_note == "ambiguous":
            issues.append(IssueCode.SIDECAR_AMBIGUOUS)
        if info.parse_error is not None:
            issues.append(IssueCode.UNPARSEABLE_AUDIO)
        elif not info.media_format.supported:
            issues.append(IssueCode.UNSUPPORTED_FORMAT)
        if not info.extension_matches_container:
            issues.append(IssueCode.CONTAINER_MISMATCH)

        tags = meta.tags
        # 标签类缺陷只对**能改写的格式**上报（见 TAG_WRITABLE_FORMATS 的说明）。
        # 不在列表里的格式会被计进 issues_skipped_unfixable，界面上单独说明。
        tag_fixable = info.media_format in TAG_WRITABLE_FORMATS
        cover_fixable = info.media_format in COVER_WRITABLE_FORMATS
        if not tag_fixable:
            skipped = 0
            if not tags.get("TITLE"):
                skipped += 1
            if not tags.get("ARTIST"):
                skipped += 1
            if not tags.get("ALBUM"):
                skipped += 1
            if not tags.get("GENRE"):
                skipped += 1
            if not tags.get("MUSICBRAINZ_TRACKID"):
                skipped += 1
            if not meta.embedded.has_cover:
                skipped += 1
            result.summary.issues_skipped_unfixable += skipped
        elif not cover_fixable and not meta.embedded.has_cover:
            # MP3 标签可写，但封面改写仍只支持 FLAC。
            result.summary.issues_skipped_unfixable += 1
        if tag_fixable and not tags.get("TITLE"):
            issues.append(IssueCode.MISSING_TITLE)
        if tag_fixable and not tags.get("ARTIST"):
            issues.append(IssueCode.MISSING_ARTIST)
        if tag_fixable and not tags.get("ALBUM"):
            issues.append(IssueCode.MISSING_ALBUM)
        # 流派是「能不能按风格找歌」的前提，也是概览与筛选的维度，
        # 所以列为可修复缺陷；已有流派（含多值）一律不报。
        if tag_fixable and not tags.get("GENRE"):
            issues.append(IssueCode.MISSING_GENRE)
        if tag_fixable and not tags.get("MUSICBRAINZ_TRACKID"):
            issues.append(IssueCode.MISSING_MBID)
        # **有意不报「缺 ISRC」。** 它是可选字段：很多发行本就没有 ISRC，
        # 而把它列为「问题」会让 22 首本来完全正常的曲目长期挂在健康页上，
        # 使用者会对整个问题列表失去信任（实际库里 22/38 的问题都是它）。
        # IssueCode.MISSING_ISRC 仍保留在枚举里——**标签修复功能仍可补它**，
        # 只是不再当作缺陷上报。

        has_artwork = meta.embedded.has_cover or dir_has_image.get(audio_path.parent, False)
        if cover_fixable and not meta.embedded.has_cover:
            issues.append(IssueCode.NO_EMBEDDED_COVER)
        if not has_artwork:
            issues.append(IssueCode.NO_COVER_ARTWORK)

        if meta.embedded.state is LyricsState.ABSENT and sidecar.is_effectively_absent:
            issues.append(IssueCode.NO_LYRICS)
        else:
            if meta.embedded.state is LyricsState.ABSENT:
                issues.append(IssueCode.MISSING_EMBEDDED)
            if sidecar.is_effectively_absent:
                issues.append(IssueCode.MISSING_SIDECAR)
        if meta.embedded.state is LyricsState.PLAIN_TEXT:
            issues.append(IssueCode.PLAIN_EMBEDDED_LYRICS)
        if LyricsState.SYNCED_SUSPICIOUS in (meta.embedded.state, sidecar.state):
            issues.append(IssueCode.SUSPICIOUS_LYRICS)

        # 内嵌与 sidecar 内容是否一致（用规范化内容指纹比较）
        if _lyrics_conflict(meta.embedded, sidecar):
            issues.append(IssueCode.LYRICS_MISMATCH)

        track = TrackAudit(
            audio=info, embedded=meta.embedded, sidecar=sidecar, tags=tags, issues=issues
        )

        st = track.effective_state.value
        by_state[st] = by_state.get(st, 0) + 1
        for issue in track.issues:
            by_issue[issue.value] = by_issue.get(issue.value, 0) + 1

        result.tracks.append(track)

    # ---- 4) 孤儿与重复 sidecar ----
    for lrc_path, _finding in sidecar_findings.items():
        if lrc_path not in consumed_sidecars:
            result.orphan_sidecars.append(_rel(lrc_path, base))
    for directory, lrc_list in sidecars_by_dir.items():
        groups: dict[str, list[Path]] = {}
        for p in lrc_list:
            groups.setdefault(scan.normalize_stem(p.stem), []).append(p)
        for _key, group in groups.items():
            if len(group) > 1:
                result.duplicate_sidecars.append(
                    (str(directory.relative_to(base)), sorted(_rel(p, base) for p in group))
                )

    summary = result.summary
    summary.root = str(base)
    summary.tracks_total = len(result.tracks)
    summary.by_format = by_format
    summary.by_lyrics_state = by_state
    summary.by_issue = by_issue
    summary.sidecar_total = len(sidecar_findings)
    summary.sidecar_empty = len(result.empty_sidecars)
    summary.tracks_without_lyrics = by_state.get(LyricsState.ABSENT.value, 0)
    summary.parse_failures = by_issue.get(IssueCode.UNPARSEABLE_AUDIO.value, 0)
    result.sidecar_states = sidecar_states
    result.elapsed_seconds = time.monotonic() - started

    result.orphan_sidecars.sort()
    result.duplicate_sidecars.sort()
    return result


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _iter_all_files(root: Path, cfg: RootConfig):
    """遍历受管根下所有文件（用于封面探测），复用 scan 的忽略规则。"""
    yield from scan._walk(root, cfg)


def find_orphan_sidecars(root: RootConfig) -> list[dict[str, Any]]:
    """列出「没有任何音频归属于它」的 .lrc，并给出**可以据此行动**的判定。

    为什么单独做成一个函数而不是复用 `audit_root().orphan_sidecars`：
        那个列表只有路径，界面据此**无法决定该做什么**。实际曲库里的两条
        孤儿都是**同一首歌的第二份 lrc**——音频已经有一个完全同名的 lrc，
        而且孤儿的内容与它**逐字节相同**：

            `09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac`
                ← [exact] `09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.lrc`
                ← 孤儿    `让你知道.lrc`（内容相同）
            `01 漂向北方.flac` ← `01 漂向北方.lrc` ← 孤儿 `1. 漂向北方.lrc`

        只看路径的话「重新配对」看着像合理的修复，实际却是错的：改名会让孤儿
        与既有 lrc **同名**，制造冲突或覆盖掉本来好好的那一份。正确动作是清理。

    **`pair_sidecars` 已经会自动配掉一切「唯一可推导」的情况**（同名、编号写法
    差异、双向唯一的包含关系）。所以走到这里还没被配走的 lrc，只可能是：
        - `duplicate`：目标音频已经有别的 lrc → 这一份是多余的，应当清理；
        - `choose`   ：有多个候选音频（包含关系有歧义），只能由人来指定给谁；
        - `unmatched`：曲库里根本没有相关音频，只能清理或留着。

    只读文件名与 lrc 内容，**不解析音频标签**，所以可以随界面按需调用。
    """
    base = root.resolved_path
    if not base.is_dir():
        return []

    audio_paths = sorted(scan.iter_audio_files(base, root))
    lrc_paths = sorted(
        p for p in _iter_all_files(base, root) if p.suffix.lower() == ".lrc"
    )
    if not lrc_paths:
        return []

    findings = {p: _read_sidecar(p) for p in lrc_paths}
    pairing = pair_sidecars(audio_paths, findings)
    owned = {side for side, _how in pairing.values() if side is not None}

    by_dir: dict[Path, list[Path]] = {}
    for audio in audio_paths:
        by_dir.setdefault(audio.parent, []).append(audio)

    def _decode(path: Path) -> str:
        try:
            return decode_lyrics_bytes(path.read_bytes())[0]
        except Exception:  # noqa: BLE001 — 编码异常不能影响整个列表
            return ""

    out: list[dict[str, Any]] = []
    for lrc in lrc_paths:
        if lrc in owned:
            continue
        target = scan.normalize_stem(lrc.stem)

        # 候选音频：同目录优先（同目录命中远比跨目录可信），再做整根兜底。
        pools: list[tuple[list[Path], str]] = []
        if by_dir.get(lrc.parent):
            pools.append((by_dir[lrc.parent], "same_dir"))
        pools.append((audio_paths, "library"))

        candidates: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for pool, scope in pools:
            for audio in pool:
                if audio in seen:
                    continue
                name = scan.normalize_stem(audio.stem)
                if not target or not name:
                    continue
                if name == target:
                    how = "normalized"
                elif target in name or name in target:
                    how = "contained"
                else:
                    continue
                seen.add(audio)
                paired = pairing.get(audio, (None, "none"))[0]
                same = False
                if paired is not None and paired != lrc:
                    mine, theirs = _decode(lrc), _decode(paired)
                    same = bool(mine) and bool(theirs) and content_hash(
                        normalize_lrc(mine)
                    ) == content_hash(normalize_lrc(theirs))
                candidates.append(
                    {
                        "rel_path": str(audio.relative_to(base)),
                        "how": how,
                        "scope": scope,
                        "paired_sidecar": (
                            str(paired.relative_to(base)) if paired is not None else None
                        ),
                        "same_content": same,
                    }
                )
            if any(c["scope"] == "same_dir" for c in candidates):
                break

        # 有候选并且候选已经有歌词 → 这一份是重复；否则交给人工指定或清理。
        duplicate = next(
            (c for c in candidates if c["paired_sidecar"] is not None), None
        )
        if duplicate is not None:
            action = "duplicate"
        elif candidates:
            action = "choose"
        else:
            action = "unmatched"

        text = _decode(lrc)
        finding = findings[lrc]
        out.append(
            {
                "rel_path": str(lrc.relative_to(base)),
                "size": lrc.stat().st_size if lrc.exists() else 0,
                "state": finding.state.value,
                "line_count": finding.parsed.line_count if finding.parsed else 0,
                "timestamp_count": (
                    finding.parsed.timestamp_count if finding.parsed else 0
                ),
                "preview": text[:600],
                "action": action,
                # 保持单数形式的 `guess_*`：界面在 choose 之外只需一个提示目标
                "guess_audio": candidates[0]["rel_path"] if candidates else None,
                "paired_sidecar": (
                    duplicate["paired_sidecar"] if duplicate is not None else None
                ),
                "same_content": bool(duplicate and duplicate["same_content"]),
                "candidates": candidates[:8],
            }
        )
    out.sort(key=lambda item: item["rel_path"])
    return out


#: 目前**能改写标签/封面**的格式。
#:
#: 为什么审计要知道这件事：封面仍只支持 FLAC。对不支持改写的格式报
#: 「缺封面」这类问题，唯一的动作是「修它」，而那个动作**必然失败**。
#: 所以：不可写的格式**不上报**这些缺陷，而是单独计数，界面上说明
#: 「有 N 项因为格式不支持改写而未计入」。这样既不制造死路，
#: 也不把事实藏起来。标签类缺陷对 FLAC / MP3 / OGG 都照常上报。
TAG_WRITABLE_FORMATS: frozenset[MediaFormat] = frozenset(
    {
        MediaFormat.FLAC,
        MediaFormat.MP3,
        MediaFormat.OGG_VORBIS,
        MediaFormat.OGG_OPUS,
    }
)
COVER_WRITABLE_FORMATS: frozenset[MediaFormat] = frozenset({MediaFormat.FLAC})


def _read_sidecar(path: Path) -> SidecarFinding:
    try:
        raw = path.read_bytes()
    except OSError:
        return SidecarFinding(exists=True, path=path, size=0, state=LyricsState.UNPARSEABLE)

    text, has_bom = decode_lyrics_bytes(raw)
    parsed = parse_lrc(text, source_label=str(path))
    state = parsed.state
    if not raw.strip():
        state = LyricsState.ABSENT
    elif not text.strip() and raw.strip():
        state = LyricsState.UNPARSEABLE

    return SidecarFinding(
        exists=True,
        path=path,
        size=len(raw),
        state=state,
        has_bom=has_bom,
        parsed=parsed,
    )


def _lyrics_conflict(embedded: EmbeddedLyricsFinding, sidecar: SidecarFinding) -> bool:
    """内嵌与 sidecar 都有有效歌词，但规范化内容不一致。

    默认「内嵌与 sidecar 两者都要」，因此两者必须内容一致；
    不一致时报告，而不是静默认为其中一个是对的。
    """
    if embedded.parsed is None or sidecar.parsed is None:
        return False
    valid = {LyricsState.SYNCED, LyricsState.SYNCED_SUSPICIOUS, LyricsState.PLAIN_TEXT}
    if embedded.state not in valid or sidecar.state not in valid:
        return False
    a = embedded.parsed.normalized_hash
    b = sidecar.parsed.normalized_hash
    if not a or not b:
        return False
    return a != b


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------


def render_audit_text(result: AuditResult, *, max_tracks: int = 15) -> str:
    s = result.summary
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add(f"Lyra 库审计报告  root={result.root_name}  {result.root_path}")
    add("=" * 72)
    add(f"音频文件        {s.tracks_total}")
    add(f"sidecar .lrc    {s.sidecar_total}  （其中 0 字节 {s.sidecar_empty}）")
    add(f"无有效歌词      {s.tracks_without_lyrics}")
    add(f"解析失败        {s.parse_failures}")
    add(f"耗时            {result.elapsed_seconds:.2f}s")

    add("")
    add("-- 容器格式（按真实内容判定）--")
    for name, count in sorted(s.by_format.items(), key=lambda kv: -kv[1]):
        add(f"  {name:16} {count}")

    add("")
    add("-- 歌词状态（内嵌与 sidecar 综合）--")
    for name, count in sorted(s.by_lyrics_state.items(), key=lambda kv: -kv[1]):
        pct = count * 100 / max(s.tracks_total, 1)
        add(f"  {name:20} {count:5}  {pct:5.1f}%")

    add("")
    add("-- 问题统计 --")
    for name, count in sorted(s.by_issue.items(), key=lambda kv: -kv[1]):
        add(f"  {name:26} {count}")

    if result.empty_sidecars:
        add("")
        add(f"-- 0 字节 sidecar（可安全填充）{len(result.empty_sidecars)} 个，前 5 --")
        for p in result.empty_sidecars[:5]:
            add(f"  {p}")

    if result.orphan_sidecars:
        add("")
        add(f"-- 孤儿 sidecar（无对应音频）{len(result.orphan_sidecars)} 个 --")
        for p in result.orphan_sidecars[:10]:
            add(f"  {p}")

    if result.duplicate_sidecars:
        add("")
        add(f"-- 疑似重复 sidecar {len(result.duplicate_sidecars)} 组 --")
        for directory, group in result.duplicate_sidecars[:10]:
            add(f"  [{directory}]")
            for g in group:
                add(f"      {g}")

    if result.bom_sidecars:
        add("")
        add(f"-- 带 BOM 的 sidecar {len(result.bom_sidecars)} 个 --")
        for p in result.bom_sidecars[:10]:
            add(f"  {p}")

    if result.unreadable:
        add("")
        add(f"-- 无法读取 {len(result.unreadable)} 项 --")
        for p, why in result.unreadable[:10]:
            add(f"  {p}: {why}")

    need = result.needs_work
    add("")
    add(f"-- 待处理曲目（需要写入歌词）{len(need)} 首，显示前 {min(len(need), max_tracks)} --")
    for track in need[:max_tracks]:
        st = track.effective_state.value
        emb = track.embedded.state.value
        side = track.sidecar.state.value if track.sidecar.exists else "-"
        add(f"  [{st:18}] 内嵌={emb:16} lrc={side:16} {track.audio.rel_path}")

    add("")
    return "\n".join(lines)
