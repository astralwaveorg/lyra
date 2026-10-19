"""修正性重命名：让扩展名与真实容器一致。

**这不是「按刮削结果重命名」。** 那个本工具永远不做。
本模块只处理一种情况：文件扩展名与它**实际的容器格式**不符。

真实需求（实际）：曲库里有 2 个文件扩展名是 `.flac`，实际是 OGG/Vorbis：

    G.E.M. 鄧紫棋/G.E.M. 鄧紫棋/蝶恋花/蝶恋花.flac      → 蝶恋花.ogg
    G.E.M. 鄧紫棋/G.E.M. 鄧紫棋/Live/Trophy Boy.flac    → Trophy Boy.ogg

安全约束：

1. **必须重新探测真实容器**，不信扩展名、也不信计划里的旧结论。
2. **目标已存在就拒绝**，绝不覆盖。
3. **检查播放列表引用**，改名前告诉你哪些列表会被打破。
4. **同目录内 `os.rename` 是原子的**，并 fsync 父目录。
5. **记录原路径，支持一键撤销。** 路径变更属于高风险操作。
6. 只改扩展名，**文件名主体不变**——因此同名 `.lrc` 的配对不受影响
   （配对规则是「去掉音频最后一个扩展名加 .lrc」，与音频扩展名无关）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import scan
from .config import RootConfig
from .db import StateStore
from .formats import detect_media_format, sniff_container
from .models import MediaFormat

__all__ = [
    "CANONICAL_EXTENSION",
    "ACCEPTED_EXTENSIONS",
    "RenamePlan",
    "RenameResult",
    "ExtensionScan",
    "plan_extension_fix",
    "scan_extension_mismatches",
    "playlist_references",
    "apply_rename",
    "undo_rename",
]

#: 每种容器对外使用的规范扩展名
CANONICAL_EXTENSION: dict[MediaFormat, str] = {
    MediaFormat.FLAC: ".flac",
    MediaFormat.MP3: ".mp3",
    MediaFormat.OGG_VORBIS: ".ogg",
    MediaFormat.OGG_OPUS: ".opus",
}

#: 被视为**无需改名**的扩展名。
#: OGG 容器装 Vorbis 用 .oga 也合法；Opus 放在 .ogg 里也很常见，
#: 为它们改名只会制造无谓的变更，所以只纠正真正误导人的情况。
ACCEPTED_EXTENSIONS: dict[MediaFormat, frozenset[str]] = {
    MediaFormat.FLAC: frozenset({".flac"}),
    MediaFormat.MP3: frozenset({".mp3"}),
    MediaFormat.OGG_VORBIS: frozenset({".ogg", ".oga"}),
    MediaFormat.OGG_OPUS: frozenset({".opus", ".ogg", ".oga"}),
}

_PLAYLIST_SUFFIXES = frozenset({".m3u", ".m3u8", ".pls", ".cue"})


@dataclass(frozen=True, slots=True)
class RenamePlan:
    source: Path
    target: Path
    rel_source: str
    rel_target: str
    media_format: MediaFormat
    reason: str

    @property
    def old_name(self) -> str:
        return self.source.name

    @property
    def new_name(self) -> str:
        return self.target.name


@dataclass(slots=True)
class ExtensionScan:
    plans: list[RenamePlan] = field(default_factory=list)
    conflicts: list[tuple[RenamePlan, str]] = field(default_factory=list)
    playlist_refs: list[tuple[str, str]] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)

    @property
    def safe_plans(self) -> list[RenamePlan]:
        blocked = {id(plan) for plan, _ in self.conflicts}
        return [p for p in self.plans if id(p) not in blocked]


def plan_extension_fix(path: Path, root: Path) -> RenamePlan | None:
    """探测真实容器；若扩展名与之不符则给出改名计划。

    用 `sniff_container` 而非 `detect_media_format`：只看内容判断容器，
    即使标签结构读不出来（例如损坏的元数据）也能正确纠正扩展名。
    探测不出来就返回 None——**不猜**。
    """
    fmt = sniff_container(path)
    if fmt is None or not fmt.supported:
        return None

    accepted = ACCEPTED_EXTENSIONS.get(fmt)
    if accepted is None:
        return None

    current = path.suffix.lower()
    if current in accepted:
        return None

    canonical = CANONICAL_EXTENSION[fmt]
    target = path.with_suffix(canonical)
    try:
        rel_source = str(path.relative_to(root))
        rel_target = str(target.relative_to(root))
    except ValueError:
        rel_source, rel_target = str(path), str(target)

    return RenamePlan(
        source=path,
        target=target,
        rel_source=rel_source,
        rel_target=rel_target,
        media_format=fmt,
        reason=(
            f"扩展名 {current} 与实际容器 {fmt.value} 不符"
            f"（以真实容器为准判定，未按扩展名推断）"
        ),
    )


def scan_extension_mismatches(root: RootConfig) -> ExtensionScan:
    """扫描一个受管根下所有扩展名与容器不符的音频。"""
    base = root.resolved_path
    result = ExtensionScan()
    if not base.is_dir():
        return result

    for path in sorted(scan.iter_audio_files(base, root)):
        sniffed = sniff_container(path)
        if sniffed is None or not sniffed.supported:
            _fmt, _handle, error = detect_media_format(path)
            if error is not None:
                result.unsupported.append((str(path.relative_to(base)), error))
            continue
        plan = plan_extension_fix(path, base)
        if plan is None:
            continue
        if plan.target.exists():
            result.conflicts.append((plan, f"目标已存在: {plan.new_name}"))
            continue
        result.plans.append(plan)

    if result.plans or result.conflicts:
        result.playlist_refs = playlist_references(
            base, root, [*(p.old_name for p in result.plans),
                         *(p.old_name for p, _ in result.conflicts)]
        )
    return result


def playlist_references(
    base: Path, root: RootConfig, names: Iterable[str]
) -> list[tuple[str, str]]:
    """在播放列表里查找对被改名文件的引用。

    改名会让播放列表里的旧文件名失效，因此必须在动手前告知，
    而不是改完让用户自己发现列表坏了。
    """
    wanted = set(names)
    if not wanted:
        return []
    hits: list[tuple[str, str]] = []

    for path in _iter_playlists(base, root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = Path(stripped).name
            if name in wanted:
                hits.append((str(path.relative_to(base)), stripped))
    return hits


def _iter_playlists(base: Path, root: RootConfig) -> Iterator[Path]:
    for path in scan._walk(base, root):  # noqa: SLF001 — 复用同一套忽略规则
        if path.suffix.lower() in _PLAYLIST_SUFFIXES:
            yield path


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RenameResult:
    ok: bool
    old_rel_path: str
    new_rel_path: str
    rename_id: int | None = None
    error: str | None = None


def apply_rename(
    store: StateStore,
    root: RootConfig,
    plan: RenamePlan,
    *,
    root_id: int | None = None,
    dry_run: bool = False,
) -> RenameResult:
    """执行改名。`dry_run=True` 时只做全部校验，不动文件。"""
    source, target = plan.source, plan.target

    if not source.is_file():
        return RenameResult(False, plan.rel_source, plan.rel_target,
                            error="源文件不存在")
    if target.exists() and target != source:
        return RenameResult(False, plan.rel_source, plan.rel_target,
                            error=f"目标已存在，拒绝覆盖: {plan.new_name}")
    if source.parent != target.parent:
        return RenameResult(False, plan.rel_source, plan.rel_target,
                            error="只支持同目录内改名（跨目录无法保证原子性）")

    # 重新探测，不信计划里的旧结论：文件可能在计划生成后被替换
    fmt = sniff_container(source)
    if fmt is None or fmt is not plan.media_format:
        return RenameResult(
            False, plan.rel_source, plan.rel_target,
            error=f"重新探测到的容器为 {fmt.value if fmt else '未知'}，"
                  f"与计划 {plan.media_format.value} 不符",
        )
    if source.suffix.lower() in ACCEPTED_EXTENSIONS.get(fmt, frozenset()):
        return RenameResult(False, plan.rel_source, plan.rel_target,
                            error="扩展名已符合规范，无需改名")

    if dry_run:
        return RenameResult(True, plan.rel_source, plan.rel_target)

    try:
        os.rename(source, target)  # 同目录内原子
    except OSError as exc:
        return RenameResult(False, plan.rel_source, plan.rel_target,
                            error=f"rename 失败: {exc}")

    _fsync_dir(target.parent)

    rename_id: int | None = None
    if root_id is not None:
        location = store.location_by_rel_path(root_id, plan.rel_source)
        rename_id = store.record_rename(
            root_id=root_id,
            old_rel_path=plan.rel_source,
            new_rel_path=plan.rel_target,
            reason=plan.reason,
            media_format=plan.media_format.value,
            location_id=int(location["id"]) if location else None,
            detail="only extension changed; stem unchanged so .lrc pairing is unaffected",
        )
        if location is not None:
            store.update_location_path(int(location["id"]), plan.rel_target)

    return RenameResult(True, plan.rel_source, plan.rel_target, rename_id=rename_id)


@dataclass(slots=True)
class GeneralRenameResult:
    ok: bool
    old_rel_path: str = ""
    new_rel_path: str = ""
    sidecar_renamed: str | None = None
    rename_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


_BAD_NAME_CHARS = ("/", "\\", "\x00")


def validate_new_name(name: str) -> str | None:
    """校验新文件名，返回错误说明（None 表示合法）。

    这是**用户直接指定文件系统名字**的地方，必须逐条挡住：
    路径分隔符、上跳、空字节、空名字、以及纯 `.`/`..`。
    """
    name = name.strip()
    if not name:
        return "文件名不能为空"
    if name in (".", ".."):
        return "文件名不合法"
    for bad in _BAD_NAME_CHARS:
        if bad in name:
            return f"文件名不能包含 {bad!r}"
    if name != Path(name).name:
        return "只能改文件名，不能包含目录部分"
    return None


def apply_general_rename(
    store: StateStore,
    root: RootConfig,
    *,
    source: Path,
    new_name: str,
    root_id: int | None = None,
    rename_sidecar: bool = True,
) -> GeneralRenameResult:
    """把任意文件改名（不限音频类型），并在可能时同步改名配对的 .lrc。

    与 `apply_rename` 的分工：那个是**扩展名修正**专用（扩展名已合规时会拒绝），
    这里是用户主动改名，任何合法文件名都接受。

    只支持同目录内改名：跨目录需要移动，无法保证原子性，而音乐库里的
    「改名」绝大多数就是同目录改文件名。
    """
    problem = validate_new_name(new_name)
    if problem:
        return GeneralRenameResult(ok=False, error=problem)
    if not source.is_file():
        return GeneralRenameResult(ok=False, error="源文件不存在")

    target = source.parent / new_name.strip()
    if target == source:
        return GeneralRenameResult(ok=False, error="新文件名与原文件相同")
    if target.exists():
        return GeneralRenameResult(
            ok=False, error=f"目标已存在，拒绝覆盖: {target.name}"
        )

    base = root.resolved_path
    rel_source = str(source.relative_to(base))
    rel_target = str(target.relative_to(base))

    # 播放列表里的引用会在改名后失效，必须**提前告知**而不是改完才说
    warnings: list[str] = []
    old_name = source.name
    for playlist in _iter_playlists(base, root):
        try:
            text = playlist.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if old_name in text:
            warnings.append(
                f"播放列表 {playlist.relative_to(base)} 引用了旧文件名，改名后需手动更新"
            )

    try:
        os.rename(source, target)  # 同目录内原子
    except OSError as exc:
        return GeneralRenameResult(ok=False, error=f"改名失败: {exc}")
    _fsync_dir(target.parent)

    # 同名 .lrc 必须跟着改，否则歌词会变成孤儿、播放器再也读不到
    sidecar_rel: str | None = None
    if rename_sidecar:
        sidecar = source.with_suffix(".lrc")
        if sidecar.is_file():
            new_sidecar = target.with_suffix(".lrc")
            if not new_sidecar.exists():
                try:
                    os.rename(sidecar, new_sidecar)
                    _fsync_dir(new_sidecar.parent)
                    sidecar_rel = str(new_sidecar.relative_to(base))
                except OSError as exc:
                    warnings.append(f"歌词文件改名失败，可能成为孤儿: {exc}")

    rename_id: int | None = None
    if root_id is not None:
        location = store.location_by_rel_path(root_id, rel_source)
        rename_id = store.record_rename(
            root_id=root_id,
            old_rel_path=rel_source,
            new_rel_path=rel_target,
            reason="manual",
            media_format=(sniff_container(target) or MediaFormat.UNSUPPORTED).value,
            location_id=int(location["id"]) if location else None,
            detail="user-requested rename",
        )
        if location is not None:
            store.update_location_path(int(location["id"]), rel_target)

    return GeneralRenameResult(
        ok=True, old_rel_path=rel_source, new_rel_path=rel_target,
        sidecar_renamed=sidecar_rel, rename_id=rename_id, warnings=warnings,
    )


def undo_rename(
    store: StateStore, root: RootConfig, rename_id: int
) -> tuple[bool, str]:
    """撤销一次改名。仅在目标仍是本事务改名结果时才执行。"""
    row = store.conn.execute("SELECT * FROM renames WHERE id=?", (rename_id,)).fetchone()
    if row is None:
        return False, "找不到该改名记录"
    if row["undone_at"]:
        return False, "该改名已被撤销"

    base = root.resolved_path
    current = base / row["new_rel_path"]
    original = base / row["old_rel_path"]

    if not current.is_file():
        return False, f"当前文件不存在: {row['new_rel_path']}"
    if original.exists():
        return False, f"原路径已被占用，停止撤销: {row['old_rel_path']}"

    try:
        os.rename(current, original)
    except OSError as exc:
        return False, f"撤销失败: {exc}"
    _fsync_dir(original.parent)

    store.mark_rename_undone(rename_id)
    location = store.location_by_rel_path(int(row["root_id"]), row["new_rel_path"])
    if location is not None:
        store.update_location_path(int(location["id"]), row["old_rel_path"])
    return True, f"已还原为 {row['old_rel_path']}"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
