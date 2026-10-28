"""曲库整理：按标签生成路径、预览、执行、逐条撤销。

## 边界

- **不监听、不自动整理。** 整理会大范围移动文件，必须由人明确触发；
  后台只做只读的重复检测（见 `dedupe.py`）。
- **不覆盖任何已存在的文件。** 目标被占用就列为冲突，留给人工判断。
- **不复制音频。** 同卷内 rename 是原子且瞬时的；跨卷移动明确拒绝，
  因为「复制 + 删除」既慢又可能在中断时留下两份。

## 目标结构

    艺人 / 专辑 / 音轨号 - 曲名.ext
    艺人 / 专辑 / 碟号-音轨号 - 曲名.ext   （多碟）
    艺人 / Unknown Album / 曲名.ext          （缺专辑）
    Unknown Artist / 专辑 / ...              （缺艺人）

多艺人用 `; ` 连接，来源优先 `ALBUMARTIST`/`ALBUM ARTISTS`，其次 `ARTISTS`。
这样合辑不会把同一张专辑拆到多个艺人目录下。
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
from .formats import read_all_tags

__all__ = [
    "build_plan",
    "execute_plan",
    "undo_organize_ops",
    "preview_cleanup",
    "cleanup_dirs",
    "safe_component",
    "target_rel_path",
]

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
#: 与 config.OrganizePolicy.template 的默认值一致；改默认值时两处同步。
DEFAULT_TEMPLATE = "{artist}/{album}/{track_no} - {title}"
MAX_COMPONENT_CHARS = 180  # bytes
MULTI_ARTIST_SEPARATOR = "; "

_BAD_PATH_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_LEADING_TRACK = re.compile(r"^\s*\d+\s*[-._]\s*")
_NUMBER = re.compile(r"^\s*(\d+)")


# ---------------------------------------------------------------------------
# 标签读取与路径生成
# ---------------------------------------------------------------------------


def _values(tags: dict[str, list[str]], *keys: str) -> list[str]:
    out: list[str] = []
    upper = {str(k).upper(): v for k, v in tags.items()}
    for key in keys:
        for value in upper.get(key.upper(), []) or []:
            text = str(value).strip()
            if text:
                out.append(text)
    return out


def _first(tags: dict[str, list[str]], *keys: str) -> str:
    values = _values(tags, *keys)
    return values[0] if values else ""


def _artist_list(tags: dict[str, list[str]]) -> list[str]:
    """艺人列表：优先专辑艺人，其次曲目艺人。

    `ARTISTS` 是 Picard 写入的多值字段；`ARTIST` 在老文件里可能是
    `A; B` 这样的一整串。只按分号拆，不按 `/` 拆——`AC/DC` 会被拆坏。
    """
    raw = _values(tags, "ALBUMARTISTS", "ALBUM ARTIST", "ALBUMARTIST")
    if not raw:
        raw = _values(tags, "ARTISTS", "ARTIST")
    out: list[str] = []
    for value in raw:
        for part in re.split(r"\s*[;；]\s*", value):
            text = part.strip()
            if text and text.casefold() not in {item.casefold() for item in out}:
                out.append(text)
    return out


def _number(tags: dict[str, list[str]], *keys: str) -> int | None:
    value = _first(tags, *keys)
    match = _NUMBER.match(value)
    if not match:
        return None
    try:
        number = int(match.group(1))
    except ValueError:
        return None
    return number if number > 0 else None


def _total(tags: dict[str, list[str]], number_keys: tuple[str, ...], total_keys: tuple[str, ...]) -> int | None:
    total = _number(tags, *total_keys)
    if total:
        return total
    raw = _first(tags, *number_keys)
    if "/" in raw:
        try:
            value = int(raw.split("/", 1)[1].strip())
        except ValueError:
            return None
        return value if value > 0 else None
    return None


def safe_component(text: str, *, fallback: str = "Unknown") -> str:
    """把标签值变成安全、跨 SMB 可用的单层路径名。

    替换控制字符与 Windows/SMB 保留字符；去掉首尾空白与结尾的点
    （Windows 不允许 `foo.`）；按字符截断到 180，给扩展名与路径留余量。
    """
    value = unicodedata.normalize("NFC", str(text or ""))
    value = _BAD_PATH_CHARS.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    if not value:
        value = fallback
    if value in (".", ".."):
        value = fallback
    # 按 UTF-8 字节截断：ext4 / SMB 限制的是字节数，不是字符数。
    # 180 字节给扩展名与同目录其它文件留出余量。
    raw = value.encode("utf-8")
    if len(raw) > MAX_COMPONENT_CHARS:
        value = raw[:MAX_COMPONENT_CHARS].decode("utf-8", "ignore")
    return value.rstrip(" .") or fallback


def _title(tags: dict[str, list[str]], path: Path) -> str:
    title = _first(tags, "TITLE")
    if title:
        return title
    stem = _LEADING_TRACK.sub("", path.stem).strip()
    return stem or path.stem


def target_rel_path(
    *,
    tags: dict[str, list[str]],
    source: Path,
    suffix: str | None = None,
    template: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """根据标签生成根内相对路径，并返回用到的字段说明。

    `template` 提供时按模板渲染（字段白名单见 config._TEMPLATE_FIELD_WHITELIST），
    未提供或等于默认模板时走内置规则——多碟加碟号、合辑降级等既有语义
    只在默认规则里实现，模板渲染是它的薄包装。
    """
    artists = _artist_list(tags)
    artist = MULTI_ARTIST_SEPARATOR.join(artists) if artists else UNKNOWN_ARTIST
    album = _first(tags, "ALBUM") or UNKNOWN_ALBUM
    title = _title(tags, source)

    track = _number(tags, "TRACKNUMBER")
    disc = _number(tags, "DISCNUMBER")
    track_total = _total(
        tags, ("TRACKNUMBER",), ("TRACKTOTAL", "TOTALTRACKS")
    )
    disc_total = _total(tags, ("DISCNUMBER",), ("DISCTOTAL", "TOTALDISCS"))
    width = 3 if (track_total or 0) >= 100 else 2

    name = f"{track:0{width}d} - {title}" if track else title
    # 多碟专辑只用音轨号会撞名（两张碟都有 01），因此带碟号前缀。
    # 单碟不前缀，保持「音轨号 - 曲名」的约定。
    multi_disc = bool(disc and ((disc_total or 1) > 1 or disc > 1))
    if multi_disc and track:
        name = f"{disc}-{track:0{width}d} - {title}"

    ext = suffix if suffix is not None else source.suffix.lower()
    fields = {
        "artist": artist,
        "artists": artists,
        "album": album,
        "title": title,
        "track": track,
        "disc": disc,
        "multi_disc": multi_disc,
    }

    if template and template.strip() != DEFAULT_TEMPLATE:
        values = {
            "artist": artist,
            "albumartist": _first(tags, "ALBUMARTIST") or _first(tags, "ALBUM ARTIST") or artist,
            "album": album,
            "title": title,
            "track_no": f"{track:0{width}d}" if track else "",
            "disc_no": str(disc) if disc else "",
            "year": _first(tags, "DATE") or _first(tags, "ORIGINALDATE") or "",
            "ext": ext,
        }
        rendered = template.format(**values)
        parts = [
            safe_component(part, fallback="track")
            for part in rendered.split("/")
            if part.strip()
        ]
        if not parts:
            parts = [safe_component(name, fallback="track") + ext]
        rel = "/".join(parts)
        if not rel.lower().endswith(ext):
            rel = f"{rel}{ext}"
        return rel, fields

    rel = "/".join(
        (
            safe_component(artist),
            safe_component(album),
            f"{safe_component(name, fallback='track')}{ext}",
        )
    )
    return rel, fields


# ---------------------------------------------------------------------------
# 目录遍历
# ---------------------------------------------------------------------------


def _clean_rel(value: str) -> str:
    text = str(value).strip().replace("\\", "/").strip("/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ValueError(f"路径不合法: {value!r}")
    return "/".join(path.parts)


def _scope_paths(
    root: RootConfig,
    subpaths: list[str] | None,
    exclude: list[str] | None,
    *,
    legacy_subpath: str = "",
) -> tuple[Path, list[Path], list[str]]:
    """把多选目录规范成根内路径；空的 subpaths 表示整个曲库。"""
    base = root.resolved_path
    raw = list(subpaths or ([legacy_subpath] if legacy_subpath else []))
    includes: list[str] = []
    for value in raw:
        rel = _clean_rel(value)
        if rel and rel not in includes:
            includes.append(rel)

    scopes: list[Path] = []
    for rel in includes or [""]:
        scope = base / rel if rel else base
        if not scope.is_dir() or scope.is_symlink():
            raise ValueError(f"目录不存在或越界: {rel or '/'}")
        if not scope.resolve().is_relative_to(base.resolve()):
            raise ValueError(f"目录不存在或越界: {rel or '/'}")
        scopes.append(scope)

    excludes: list[str] = []
    for value in exclude or []:
        rel = _clean_rel(value)
        if not rel or rel in excludes:
            continue
        target = base / rel
        # 排除项可能已被删除；只要不是越界，就不让它阻止本次整理。
        if target.exists() and (
            target.is_symlink() or not target.resolve().is_relative_to(base.resolve())
        ):
            raise ValueError(f"排除目录越界: {rel}")
        excludes.append(rel)
    return base, scopes, excludes


def _excluded(rel: str, excludes: list[str]) -> bool:
    return any(rel == item or rel.startswith(item + "/") for item in excludes)


def _walk_dirs(base: Path, root: RootConfig) -> list[Path]:
    """返回受管根内所有真实目录（不跟随符号链接、不跨挂载点、遵守忽略规则）。"""
    ignore = scan.IGNORE_DIR_NAMES | set(root.ignore_dirs)
    try:
        root_dev = base.stat().st_dev
    except OSError:
        return []
    out: list[Path] = []
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name in ignore:
                    continue
                if entry.stat(follow_symlinks=False).st_dev != root_dev:
                    continue
                path = Path(entry.path)
                out.append(path)
                stack.append(path)
            except OSError:
                continue
    return out


def _files_under(directory: Path, base: Path, root: RootConfig) -> tuple[int, int]:
    """(文件总数, 音频数)，递归但不跟随符号链接。"""
    files = 0
    audio = 0
    ignore = scan.IGNORE_DIR_NAMES | set(root.ignore_dirs)
    from .formats import AUDIO_EXTENSIONS

    for current, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ignore and not (Path(current) / d).is_symlink()]
        for name in names:
            path = Path(current) / name
            if path.is_symlink():
                continue
            # 这里**不能**跳过忽略文件：`.DS_Store` 仍在磁盘上，
            # 把它当成「空目录」会让随后的 rmdir 失败。计入 files，
            # 这类目录会走「整目录移入回收站」而不是假装为空。
            files += 1
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                audio += 1
    return files, audio


def _file_set_under(directory: Path, base: Path, root: RootConfig) -> set[str]:
    """目录下所有文件的根内相对路径（用于预测移动后是否会变空）。"""
    out: set[str] = set()
    ignore = scan.IGNORE_DIR_NAMES | set(root.ignore_dirs)
    for current, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [
            d for d in dirs
            if d not in ignore and not (Path(current) / d).is_symlink()
        ]
        for name in names:
            path = Path(current) / name
            if path.is_symlink():
                continue
            try:
                out.add(str(path.relative_to(base)))
            except ValueError:
                continue
    return out


def preview_cleanup(
    root: RootConfig,
    *,
    subpaths: list[str] | None = None,
    exclude: list[str] | None = None,
    subpath: str = "",
) -> list[dict[str, Any]]:
    """列出所选范围内「没有任何音频」的目录，按深度从深到浅。

    `files=0` 是真空目录，可以直接 rmdir；
    `files>0` 说明只剩封面/歌词/说明文件，删除会丢内容，因此执行时
    整目录移入回收站，而不是 rmdir。
    """
    base, scopes, excludes = _scope_paths(
        root, subpaths, exclude, legacy_subpath=subpath
    )
    seen: dict[str, Path] = {}
    for scope in scopes:
        for directory in _walk_dirs(scope, root):
            rel = str(directory.relative_to(base))
            if _excluded(rel, excludes):
                continue
            seen[rel] = directory

    out: list[dict[str, Any]] = []
    for rel, directory in seen.items():
        files, audio = _files_under(directory, base, root)
        if audio:
            continue
        out.append(
            {
                "rel_path": rel,
                "depth": len(Path(rel).parts),
                "files": files,
                "kind": "empty" if files == 0 else "no_audio",
            }
        )
    out.sort(key=lambda item: (-item["depth"], item["rel_path"]))
    return out


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BuildResult:
    plan: dict[str, Any] = field(default_factory=dict)


def build_plan(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    subpaths: list[str] | None = None,
    exclude: list[str] | None = None,
    subpath: str = "",
    progress: Any = None,
) -> dict[str, Any]:
    """生成整理预览。只读，不改任何文件。

    `subpaths` 可多选；为空表示整个曲库。`exclude` 排除其自身及全部后代。
    """
    base, scopes, excludes = _scope_paths(
        root, subpaths, exclude, legacy_subpath=subpath
    )
    found: dict[str, Path] = {}
    for scope in scopes:
        for path in scan.iter_audio_files(scope, root):
            try:
                rel = str(path.relative_to(base))
            except ValueError:
                continue
            if _excluded(rel, excludes):
                continue
            found[rel] = path
    all_files = [found[rel] for rel in sorted(found)]
    moves: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    sources: set[str] = set()

    for index, path in enumerate(all_files):
        if progress and index and index % 100 == 0:
            progress(f"已分析 {index}/{len(all_files)} 个文件…")
        try:
            rel = str(path.relative_to(base))
        except ValueError:
            continue
        tags, error = read_all_tags(path)
        if error:
            skipped.append({"old_rel_path": rel, "reason": f"读不到标签：{error}"})
            continue
        try:
            new_rel, fields = target_rel_path(tags=tags, source=path)
        except Exception as exc:  # noqa: BLE001 — 单个文件不能拖垮整次预览
            skipped.append({"old_rel_path": rel, "reason": f"无法生成路径：{exc}"})
            continue
        if new_rel == rel:
            skipped.append(
                {"old_rel_path": rel, "new_rel_path": new_rel, "reason": "路径已经正确"}
            )
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        sources.add(rel)
        sidecar = path.with_suffix(".lrc")
        new_sidecar = str(Path(new_rel).with_suffix(".lrc"))
        moves.append(
            {
                "old_rel_path": rel,
                "new_rel_path": new_rel,
                "old_name": path.name,
                "new_name": Path(new_rel).name,
                "size": size,
                "format": Path(rel).suffix.lower().lstrip("."),
                "has_sidecar": sidecar.is_file(),
                "sidecar_old": str(sidecar.relative_to(base)) if sidecar.is_file() else "",
                "sidecar_new": new_sidecar if sidecar.is_file() else "",
                **fields,
            }
        )

    # 目标冲突：已存在且不是「本次也要移走的源文件」；或两个源映射到同一个目标。
    target_counts: dict[str, int] = {}
    for move in moves:
        target_counts[move["new_rel_path"]] = target_counts.get(move["new_rel_path"], 0) + 1
    for move in moves:
        target = base / move["new_rel_path"]
        if target_counts[move["new_rel_path"]] > 1:
            move["status"] = "conflict"
            move["reason"] = "多个文件会映射到同一路径"
            continue
        if target.exists() and move["new_rel_path"] not in sources:
            move["status"] = "conflict"
            move["reason"] = "目标已存在，拒绝覆盖"
            continue
        if target.exists() and move["new_rel_path"] in sources:
            move["status"] = "chain"
            move["reason"] = "目标位置的文件也会移动，按依赖顺序执行"
            continue
        move["status"] = "move"
        move["reason"] = ""

    executable = [m for m in moves if m["status"] in ("move", "chain")]
    conflicts = [m for m in moves if m["status"] == "conflict"]
    dirs = preview_cleanup(root, subpaths=subpaths, exclude=exclude, subpath=subpath)

    # 预测「移动后会变空」的旧目录，让它们在预览里出现、可勾选。
    # 不在执行时偷偷删除用户没看过的目录。
    moving: set[str] = set()
    arriving: set[str] = set()
    for move in executable:
        moving.add(move["old_rel_path"])
        arriving.add(move["new_rel_path"])
        if move.get("sidecar_old"):
            moving.add(str(move["sidecar_old"]))
        if move.get("sidecar_new"):
            arriving.add(str(move["sidecar_new"]))
    known = {item["rel_path"] for item in dirs}
    candidates: dict[str, Path] = {}
    for scope in scopes:
        for directory in _walk_dirs(scope, root):
            rel = str(directory.relative_to(base))
            if _excluded(rel, excludes):
                continue
            candidates[rel] = directory
    for rel, directory in candidates.items():
        if rel in known:
            continue
        files = _file_set_under(directory, base, root)
        if not files or not files.issubset(moving):
            continue
        if any(item == rel or item.startswith(rel + "/") for item in arriving):
            continue
        dirs.append(
            {"rel_path": rel, "depth": len(Path(rel).parts), "files": 0, "kind": "after_move"}
        )
    dirs.sort(key=lambda item: (-item["depth"], item["rel_path"]))

    already_ok = sum(1 for item in skipped if item["reason"] == "路径已经正确")
    plan = {
        "version": 1,
        "root": root.name,
        "subpaths": [
            str(scope.relative_to(base)) for scope in scopes if scope != base
        ],
        "exclude": excludes,
        "subpath": subpath,
        "created_at": time.time(),
        "moves": moves,
        "executable": executable,
        "conflicts": conflicts,
        "skipped": skipped,
        "dirs": dirs,
        "summary": {
            "total": len(all_files),
            "move": len(executable),
            "conflict": len(conflicts),
            "already_ok": already_ok,
            "skipped": len(skipped) - already_ok,
            "empty_dirs": sum(1 for d in dirs if d["kind"] == "empty"),
            "no_audio_dirs": sum(1 for d in dirs if d["kind"] == "no_audio"),
            "after_move_dirs": sum(1 for d in dirs if d["kind"] == "after_move"),
        },
    }
    plan["plan_id"] = uuid.uuid4().hex[:16]
    return plan


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _rename_with_sidecar(
    *, base: Path, old_rel: str, new_rel: str, source_override: Path | None = None
) -> tuple[bool, str | None, str | None, str | None]:
    """移动音频，并在可能时带上同名 .lrc。返回 (ok, sidecar_old, sidecar_new, warning)。"""
    source = source_override or (base / old_rel)
    target = base / new_rel
    if not source.is_file():
        return False, None, None, "源文件不存在"
    if target.exists():
        return False, None, None, "目标已存在"
    base_resolved = base.resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 标签值不可能含 `..`，但目标父目录可能是既有符号链接。
        # 必须解析后确认仍在受管根内，否则会把文件写到根外。
        if not target.parent.resolve().is_relative_to(base_resolved):
            return False, None, None, "目标目录越界"
    except OSError as exc:
        return False, None, None, f"创建目标目录失败：{exc}"
    try:
        os.rename(source, target)
    except OSError as exc:
        return False, None, None, f"移动失败：{exc}"

    if source_override is not None:
        # 环形交换的临时文件：歌词留在原地会与新位置冲突，交给人工处理
        warning = "环形改动，歌词文件未随之移动"
        _fsync_dir(target.parent)
        return True, None, None, warning
    sidecar_old = str(Path(old_rel).with_suffix(".lrc"))
    sidecar_new = str(Path(new_rel).with_suffix(".lrc"))
    src_lrc = base / sidecar_old
    dst_lrc = base / sidecar_new
    warning = None
    if src_lrc.is_file():
        if dst_lrc.exists():
            warning = f"目标歌词已存在，未移动：{sidecar_new}"
            sidecar_old = sidecar_new = ""
        else:
            try:
                os.rename(src_lrc, dst_lrc)
            except OSError as exc:
                warning = f"歌词移动失败：{exc}"
                sidecar_old = sidecar_new = ""
    else:
        sidecar_old = sidecar_new = ""
    _fsync_dir(target.parent)
    return True, sidecar_old or None, sidecar_new or None, warning


def execute_plan(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    plan_row: Any,
    *,
    dirs: list[str] | None = None,
) -> dict[str, Any]:
    """执行预览计划。`dirs` 是用户勾选要清理的目录相对路径。"""
    base = root.resolved_path
    payload = json.loads(str(plan_row["payload_json"]))
    batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    result: dict[str, Any] = {
        "ok": True,
        "batch_id": batch_id,
        "moved": 0,
        "failed": 0,
        "errors": [],
        "dirs_removed": 0,
        "dirs_trashed": 0,
        "ops": [],
    }

    # 重新验证，但**不重新计算目标**：执行的是用户看到的那一份。
    pending: list[dict[str, Any]] = []
    for move in payload.get("executable") or []:
        old_rel = str(move.get("old_rel_path") or "")
        new_rel = str(move.get("new_rel_path") or "")
        if not old_rel or not new_rel:
            continue
        if not (base / old_rel).is_file():
            result["failed"] += 1
            result["errors"].append(f"{old_rel}: 源文件已不存在")
            continue
        if (base / new_rel).exists() and new_rel != old_rel:
            # 目标可能是本次也要移走的另一个源，按依赖关系处理
            pass
        pending.append(move)

    pending_olds = {m["old_rel_path"] for m in pending}
    processed: set[str] = set()

    def _do(move: dict[str, Any], *, source_override: Path | None = None) -> bool:
        old_rel, new_rel = move["old_rel_path"], move["new_rel_path"]
        ok, lrc_old, lrc_new, warning = _rename_with_sidecar(
            base=base, old_rel=old_rel, new_rel=new_rel, source_override=source_override
        )
        if not ok:
            result["failed"] += 1
            result["errors"].append(f"{old_rel}: {warning}")
            return False
        op_id = store.record_organize_op(
            root_id=int(plan_row["root_id"]),
            batch_id=batch_id,
            op_kind="move",
            old_rel_path=old_rel,
            new_rel_path=new_rel,
            detail=json.dumps(
                {"sidecar_old": lrc_old, "sidecar_new": lrc_new, "warning": warning},
                ensure_ascii=False,
            ),
        )
        location = store.location_by_rel_path(int(plan_row["root_id"]), old_rel)
        if location is not None:
            store.update_location_path(int(location["id"]), new_rel)
        result["moved"] += 1
        result["ops"].append(op_id)
        if warning:
            result["errors"].append(f"{old_rel}: {warning}")
        processed.add(old_rel)
        return True

    # 依赖顺序：目标仍是待处理源文件时先处理那个源；成环时用临时名交换。
    while pending:
        progress = False
        for move in list(pending):
            target_rel = move["new_rel_path"]
            if (
                target_rel in pending_olds
                and target_rel not in processed
                and target_rel != move["old_rel_path"]
            ):
                continue
            if (base / target_rel).exists() and target_rel not in processed:
                result["failed"] += 1
                result["errors"].append(
                    f"{move['old_rel_path']}: 目标已存在，拒绝覆盖 {target_rel}"
                )
                pending.remove(move)
                progress = True
                continue
            _do(move)
            pending.remove(move)
            progress = True
        if pending and not progress:
            # 环形依赖：先把剩余源文件改成临时名，再统一落位。
            temps: list[tuple[dict[str, Any], Path]] = []
            for move in pending:
                source = base / move["old_rel_path"]
                temp = source.with_name(f".lyra-move-{uuid.uuid4().hex}.tmp")
                try:
                    os.rename(source, temp)
                    temps.append((move, temp))
                except OSError as exc:
                    result["failed"] += 1
                    result["errors"].append(f"{move['old_rel_path']}: 交换失败 {exc}")
            for move, temp in temps:
                target = base / move["new_rel_path"]
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _do(move, source_override=temp)
                except OSError as exc:
                    result["failed"] += 1
                    result["errors"].append(f"{move['old_rel_path']}: 落位失败 {exc}")
            pending.clear()
            break

    # 目录清理：只处理用户勾选的；深目录先处理，祖先被移走后跳过后代。
    # 「移动后变空」的旧目录也在这份列表里，由用户决定是否勾选。
    selected = set(dirs or [])
    dir_items = [d for d in (payload.get("dirs") or []) if d["rel_path"] in selected]
    dir_items.sort(key=lambda item: item["depth"], reverse=True)
    moved_ancestors: list[str] = []
    for item in dir_items:
        rel = str(item["rel_path"])
        if any(rel == anc or rel.startswith(anc + "/") for anc in moved_ancestors):
            continue
        path = base / rel
        if not path.is_dir():
            continue
        files, audio = _files_under(path, base, root)
        if audio:
            result["errors"].append(f"{rel}: 已含音频，跳过清理")
            continue
        if files == 0:
            try:
                path.rmdir()
                op_id = store.record_organize_op(
                    root_id=int(plan_row["root_id"]),
                    batch_id=batch_id,
                    op_kind="rmdir",
                    old_rel_path=rel,
                )
                result["dirs_removed"] += 1
                result["ops"].append(op_id)
            except OSError as exc:
                result["errors"].append(f"{rel}: 删除空目录失败 {exc}")
        elif item["kind"] in ("empty", "after_move"):
            # 预览时是空的，执行时突然有文件：只可能是别人刚放进去的。
            # 这种目录绝不能当「无音频目录」整目录移走。
            result["errors"].append(f"{rel}: 预览后出现新文件，已跳过，不清理")
        else:
            try:
                trash_rel = move_to_trash(cfg, root, path, batch_id)
                op_id = store.record_organize_op(
                    root_id=int(plan_row["root_id"]),
                    batch_id=batch_id,
                    op_kind="delete",
                    old_rel_path=rel,
                    trash_rel_path=trash_rel,
                )
                result["dirs_trashed"] += 1
                result["ops"].append(op_id)
                moved_ancestors.append(rel)
            except OSError as exc:
                result["errors"].append(f"{rel}: 移入回收站失败 {exc}")

    store.consume_organize_plan(str(plan_row["id"]))
    result["ok"] = result["failed"] == 0 and not result["errors"]
    return result


def cleanup_dirs(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    subpaths: list[str] | None = None,
    exclude: list[str] | None = None,
    subpath: str = "",
    paths: list[str],
) -> dict[str, Any]:
    """清理预览里勾选的目录。只接受当前确实「没有音频」的路径。"""
    base = root.resolved_path
    root_id = store.get_root_id(root.name)
    if root_id is None:
        root_id = store.upsert_root(
            name=root.name, role=root.role.value, path=str(root.resolved_path),
            enabled=root.enabled, read_only=root.read_only,
        )
    current = {
        item["rel_path"]: item
        for item in preview_cleanup(
            root, subpaths=subpaths, exclude=exclude, subpath=subpath
        )
    }
    selected = [path for path in dict.fromkeys(paths) if path in current]
    selected.sort(key=lambda path: current[path]["depth"], reverse=True)
    batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    result: dict[str, Any] = {
        "ok": True,
        "batch_id": batch_id,
        "removed": 0,
        "trashed": 0,
        "errors": [],
        "ops": [],
    }
    moved_ancestors: list[str] = []
    for rel in selected:
        if any(rel == anc or rel.startswith(anc + "/") for anc in moved_ancestors):
            continue
        path = base / rel
        if not path.is_dir():
            continue
        files, audio = _files_under(path, base, root)
        if audio:
            result["errors"].append(f"{rel}: 已含音频，跳过")
            continue
        if files == 0:
            try:
                path.rmdir()
                result["ops"].append(
                    store.record_organize_op(
                        root_id=root_id,
                        batch_id=batch_id,
                        op_kind="rmdir",
                        old_rel_path=rel,
                    )
                )
                result["removed"] += 1
            except OSError as exc:
                result["errors"].append(f"{rel}: {exc}")
        elif current[rel]["kind"] == "empty":
            result["errors"].append(f"{rel}: 预览后出现新文件，已跳过，不清理")
        else:
            try:
                trash_rel = move_to_trash(cfg, root, path, batch_id)
                result["ops"].append(
                    store.record_organize_op(
                        root_id=root_id,
                        batch_id=batch_id,
                        op_kind="delete",
                        old_rel_path=rel,
                        trash_rel_path=trash_rel,
                        detail=json.dumps({"files": files}, ensure_ascii=False),
                    )
                )
                result["trashed"] += 1
                moved_ancestors.append(rel)
            except OSError as exc:
                result["errors"].append(f"{rel}: {exc}")
    result["ok"] = not result["errors"]
    return result


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------


def _root_for_row(cfg: Config, row: Any) -> RootConfig | None:
    return next((r for r in cfg.roots if r.name == row["root_name"]), None)


def undo_organize_op(store: StateStore, cfg: Config, row: Any) -> tuple[bool, str]:
    if row["undone_at"]:
        return False, "该操作已撤销"
    root = _root_for_row(cfg, row)
    if root is None:
        return False, "该记录所属受管根当前未配置"
    base = root.resolved_path
    kind = str(row["op_kind"])

    if kind == "move":
        current = base / str(row["new_rel_path"])
        original = base / str(row["old_rel_path"])
        if not current.is_file():
            return False, f"当前文件不存在：{row['new_rel_path']}"
        if original.exists():
            return False, f"原路径已被占用：{row['old_rel_path']}"
        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(current, original)
        except OSError as exc:
            return False, f"撤销移动失败：{exc}"
        detail: dict[str, Any] = {}
        try:
            detail = json.loads(str(row["detail"] or "{}"))
        except (TypeError, ValueError):
            detail = {}
        # 歌词跟着回去
        lrc_old, lrc_new = detail.get("sidecar_old"), detail.get("sidecar_new")
        if lrc_old and lrc_new:
            src = base / str(lrc_new)
            dst = base / str(lrc_old)
            if src.is_file() and not dst.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(src, dst)
                except OSError:
                    pass
        _fsync_dir(original.parent)
        location = store.location_by_rel_path(int(row["root_id"]), str(row["new_rel_path"]))
        if location is not None:
            store.update_location_path(int(location["id"]), str(row["old_rel_path"]))
        store.mark_organize_undone(int(row["id"]))
        return True, f"已还原为 {row['old_rel_path']}"

    if kind == "rmdir":
        path = base / str(row["old_rel_path"])
        if path.exists():
            return False, f"目录已存在：{row['old_rel_path']}"
        try:
            path.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            return False, f"重建目录失败：{exc}"
        store.mark_organize_undone(int(row["id"]))
        return True, f"已重建目录 {row['old_rel_path']}"

    if kind == "delete":
        trash_rel = str(row["trash_rel_path"] or "")
        if not trash_rel:
            return False, "记录里没有回收站路径"
        trash_base = trash_root(cfg) / str(row["batch_id"]) / trash_rel
        dest = base / str(row["old_rel_path"])
        if not trash_base.exists():
            return False, "回收站里已找不到该内容"
        if dest.exists():
            return False, f"原位置已被占用：{row['old_rel_path']}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(trash_base), str(dest))
        except OSError as exc:
            return False, f"从回收站还原失败：{exc}"
        # 删除音频时歌词是一起移走的，还原也必须一起，否则歌词留在回收站
        try:
            detail = json.loads(str(row["detail"] or "{}"))
        except (TypeError, ValueError):
            detail = {}
        sidecar = str(detail.get("sidecar") or "")
        if sidecar and dest.is_file():
            lrc_target = dest.with_name(Path(sidecar).name)
            lrc_trash = trash_root(cfg) / str(row["batch_id"]) / sidecar
            if lrc_trash.is_file() and not lrc_target.exists():
                try:
                    lrc_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(lrc_trash), str(lrc_target))
                except OSError:
                    pass
        store.mark_organize_undone(int(row["id"]))
        return True, f"已从回收站还原 {row['old_rel_path']}"

    return False, f"未知的整理操作类型：{kind}"


def undo_organize_ops(
    store: StateStore, cfg: Config, op_ids: list[int]
) -> dict[str, Any]:
    """批量撤销。按时间倒序处理，单项失败不影响其它项。"""
    rows = []
    for op_id in op_ids:
        row = store.organize_op(int(op_id))
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (float(r["created_at"] or 0), int(r["id"])), reverse=True)
    ok = 0
    failed = 0
    messages: list[str] = []
    for row in rows:
        success, message = undo_organize_op(store, cfg, row)
        if success:
            ok += 1
        else:
            failed += 1
            messages.append(f"#{row['id']} {message}")
    return {"ok": failed == 0, "undone": ok, "failed": failed, "messages": messages}


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
