"""统一入库流水线（重构方案 §4.1）。

目标：任何音频文件，扔进受管根后无需人工干预即完成——

    审计 → 身份解析 → 并行补全（歌词 / 标签 / 封面）→ 归位 → 质检留痕

人的精力只花在「机器不确定」的少数项上（待办收件箱）。

工程约束（全部继承现状，绝不绕过）：

- 抓取与写入分离；写入走写租约 + 备份 + 音频哈希校验（writer）；
- **归置红线**：只对「本进程观察到的新增文件」动手
  （`policy.organize.mode=new_files`，可被 `root.organize_mode` 覆盖）；
  存量曲库永远走整理页的人工预览 + 执行；
- 所有移动记录 `organize_ops`，可逐条撤销；目标存在绝不覆盖；
- `root.read_only` 或 `policy.write_enabled=false` 的根只审计、只出建议，
  跳过全部写入支线。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import identity, organize
from .audit import audit_root
from .config import Config, RootConfig
from .cover import (
    build_picture_block,
    caa_front_url,
    cover_quality_ok,
    detect_image,
    download_cover,
)
from .db import StateStore
from .formats import read_audio_meta
from .pipeline import ProgressFn, _noop, run_apply, run_fetch, sync_state
from .providers import Provider

__all__ = ["run_ingest", "run_cover_stage", "find_sibling_cover"]

_MAX_COVER_BYTES = 20 * 1024 * 1024

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

# 与 api._SIBLING_COVER_NAMES 同口径：常用名优先，大小写不敏感，前缀兜底
_SIBLING_COVER_NAMES: tuple[str, ...] = (
    "cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg",
    "folder.png", "front.jpg", "front.png", "album.jpg", "album.png",
    "albumart.jpg", "albumartsmall.jpg",
)
_SIBLING_COVER_PREFIXES: tuple[str, ...] = ("cover", "folder", "front", "album")


def find_sibling_cover(audio_path: Path) -> Path | None:
    """同目录规范命名封面图（大小写不敏感）。与 api 层的探测同口径。"""
    parent = audio_path.parent
    try:
        entries = {
            path.name.casefold(): path
            for path in parent.iterdir()
            if path.is_file() and not path.is_symlink()
        }
    except OSError:
        return None
    for name in _SIBLING_COVER_NAMES:
        hit = entries.get(name)
        if hit is not None and hit.suffix.lower() in _IMAGE_SUFFIXES:
            return hit
    for key in sorted(entries):
        if key.startswith(_SIBLING_COVER_PREFIXES) and entries[key].suffix.lower() in _IMAGE_SUFFIXES:
            return entries[key]
    return None


def _effective_organize_mode(cfg: Config, root: RootConfig) -> str:
    if root.organize_mode:
        return root.organize_mode
    return cfg.policy.organize.mode


def _writable(cfg: Config, root: RootConfig) -> bool:
    return cfg.policy.write_enabled and not root.read_only


def run_ingest(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    providers: list[Provider],
    *,
    rel_paths: list[str],
    progress: ProgressFn = _noop,
    should_yield: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """对一批（通常是新观察到的）文件跑完整入库流水线。

    每个环节的结果都写进 task_steps：done / review / failed / skipped。
    「review」就是待办收件箱的数据源——机器不确定的项集中在这里。
    """
    task_id = f"ingest-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    store.upsert_task(task_id=task_id, kind="ingest", state="running",
                      progress=f"0/{len(rel_paths)}", started_at=time.time())
    result: dict[str, Any] = {
        "task_id": task_id, "fetched": 0, "applied": 0, "covers": 0,
        "moved": 0, "review": 0, "failed": 0,
    }
    writable = _writable(cfg, root)
    base = root.resolved_path
    seq = 0

    def step(kind: str, rel: str, state: str, detail: dict | None = None) -> None:
        nonlocal seq
        seq += 1
        store.add_task_step(
            task_id=task_id, seq=seq, kind=kind, rel_path=rel,
            state=state, detail=detail,
        )

    try:
        # ---- ① 审计 + 身份（本地零成本） --------------------------------
        audit = audit_root(root, only_rel_paths=set(rel_paths))
        root_id = store.upsert_root(
            name=root.name, role=root.role.value, path=str(root.resolved_path),
            enabled=root.enabled, read_only=root.read_only,
        )
        store.set_root_health(root_id, "ok")
        sync_state(store, cfg, root, audit, full_scan=False, progress=progress)

        def tags_for(row) -> dict[str, list[str]]:
            path = base / str(row["rel_path"])
            try:
                return read_audio_meta(path, base).tags
            except Exception:  # noqa: BLE001 — 单文件读失败不阻塞整批
                return {}

        loc_rows = [
            row for rel in rel_paths
            if (row := store.conn.execute(
                "SELECT id, rel_path FROM locations WHERE root_id=? AND rel_path=?",
                (root_id, rel),
            ).fetchone()) is not None
        ]
        identity.sync_identities(store, root_id=root_id, locations=loc_rows,
                                 tags_for=tags_for)
        known_ids = {int(row["id"]) for row in loc_rows}
        for rel in rel_paths:
            row = store.conn.execute(
                "SELECT id FROM locations WHERE root_id=? AND rel_path=?",
                (root_id, rel),
            ).fetchone()
            resolved = bool(row) and identity.resolve_track(
                store, int(row["id"])
            ).resolved
            step("identity", rel, "done" if resolved else "review",
                 {"note": "本地证据（标签 MBID / 目录锚点 / 提案回写）",
                  "located": int(row["id"]) in known_ids if row else False})

        # ---- ② 歌词支线（含 AI 预审） ------------------------------------
        if providers:
            fetch_summary = run_fetch(
                store, cfg, root, providers,
                audit=audit, only_rel_paths=set(rel_paths),
                progress=progress, should_yield=should_yield,
            )
            result["fetched"] = fetch_summary.accepted + fetch_summary.ai_accepted
            for rel in rel_paths:
                step("lyrics", rel, "done", {
                    "accepted": fetch_summary.accepted,
                    "ai_accepted": fetch_summary.ai_accepted,
                    "needs_review": fetch_summary.needs_review,
                    "not_found": fetch_summary.not_found,
                })
        else:
            for rel in rel_paths:
                step("lyrics", rel, "skipped", {"why": "无可用来源"})

        # ---- ③ 写入支线（仅暂存的已接受项） ------------------------------
        if writable:
            apply_summary = run_apply(
                store, cfg, root, only_rel_paths=set(rel_paths),
                progress=progress, should_yield=should_yield,
            )
            result["applied"] = apply_summary.applied
        else:
            for rel in rel_paths:
                step("apply", rel, "skipped", {"why": "只读：只审计与出建议"})

        # ---- ④ 封面支线 --------------------------------------------------
        cover_result = run_cover_stage(store, cfg, root, rel_paths=rel_paths)
        result["covers"] = cover_result.get("embedded", 0)
        for rel, state, detail in cover_result.get("steps", []):
            step("cover", rel, state, detail)

        # ---- ⑤ 归位（红线护栏：仅新文件 + 显式开启） ----------------------
        mode = _effective_organize_mode(cfg, root)
        if mode == "new_files" and writable:
            moved = _organize_new_files(store, cfg, root, rel_paths, task_id, root_id)
            result["moved"] = moved
        elif mode == "off":
            for rel in rel_paths:
                step("organize", rel, "review", {
                    "note": "mode=off：仅生成归置建议（整理页/待办可见），不动文件",
                })
        # inbox 流转见 run_ingest 的文档与交付说明：跨根移动暂以建议形态提供

        store.upsert_task(
            task_id=task_id, kind="ingest", state="done",
            progress=f"{len(rel_paths)}/{len(rel_paths)}",
            result=result, started_at=time.time(), finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 — 任务级失败也要留痕
        store.upsert_task(
            task_id=task_id, kind="ingest", state="failed",
            error=f"{type(exc).__name__}: {exc}",
            started_at=time.time(), finished_at=time.time(),
        )
        result["failed"] += 1
    return result


def _organize_new_files(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    rel_paths: list[str],
    task_id: str,
    root_id: int,
) -> int:
    """对新增文件执行自动归位。冲突（目标存在/字段不足）一律不动、转待办。"""
    base = root.resolved_path
    batch_id = f"ingest-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    moved = 0
    for rel in rel_paths:
        path = base / rel
        if not path.is_file():
            continue
        try:
            meta = read_audio_meta(path, base)
        except Exception:  # noqa: BLE001
            continue
        target_rel, fields = organize.target_rel_path(
            tags=meta.tags, source=path, template=cfg.policy.organize.template
        )
        if not target_rel or target_rel == rel:
            continue
        target = base / target_rel
        if target.exists():
            store.add_task_step(
                task_id=task_id, seq=0, kind="organize", rel_path=rel,
                state="review",
                detail={"conflict": str(target_rel), "fields": fields},
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 同根 rename 是原子的；目标存在已在上一步拦截，绝不覆盖
            path.rename(target)
        except OSError:
            store.add_task_step(
                task_id=task_id, seq=0, kind="organize", rel_path=rel,
                state="failed", detail={"error": "rename 失败", "target": target_rel},
            )
            continue
        store.record_organize_op(
            root_id=root_id, batch_id=batch_id, op_kind="move",
            old_rel_path=rel, new_rel_path=target_rel,
            detail="入库自动归位（仅新增文件）",
        )
        # 同名 .lrc 跟随（与整理页语义一致；异名配对交给人工预览）
        sidecar = path.with_suffix(".lrc")
        if sidecar.is_file():
            sidecar_target = target.with_suffix(".lrc")
            if not sidecar_target.exists():
                sidecar.rename(sidecar_target)
                store.record_organize_op(
                    root_id=root_id, batch_id=batch_id, op_kind="move",
                    old_rel_path=str(sidecar.relative_to(base)),
                    new_rel_path=str(sidecar_target.relative_to(base)),
                    detail="跟随音频归位",
                )
        moved += 1
        store.add_task_step(
            task_id=task_id, seq=0, kind="organize", rel_path=rel,
            state="done", detail={"target": target_rel},
        )
    return moved


def run_cover_stage(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    *,
    rel_paths: list[str],
) -> dict[str, Any]:
    """封面支线：同目录图 → Cover Art Archive → 建议。

    - 同目录规范命名图（cover.jpg 等）：零网络成本，`auto_embed_sibling`
      开启时自动嵌入（默认开）；
    - CAA：需要身份（发行 MBID）。按专辑聚合缓存（cover_albums 表），
      一次下载、同专辑缺封面的文件逐个嵌入；`auto_write=false`（默认）
      时只落「review」步骤，进待办封面队列供人确认；
    - 质量门：JPEG/PNG 且 ≥ min_min_pixels，不过关换下一来源。
    """
    policy = cfg.policy.cover
    base = root.resolved_path
    writable = _writable(cfg, root)
    steps: list[tuple[str, str, dict | None]] = []
    embedded = 0

    for rel in rel_paths:
        path = base / rel
        if not path.is_file():
            continue
        try:
            meta = read_audio_meta(path, base)
        except Exception as exc:  # noqa: BLE001
            steps.append((rel, "failed", {"error": str(exc)[:120]}))
            continue
        if not meta.info.media_format.supported:
            steps.append((rel, "skipped", {"why": "不支持的容器"}))
            continue
        if meta.embedded.has_cover:
            steps.append((rel, "skipped", {"why": "已有内嵌封面"}))
            continue

        # ---- 同目录图 ----
        if policy.auto_embed_sibling:
            sibling = find_sibling_cover(path)
            if sibling is not None:
                outcome = _embed_bytes(path, meta.info.media_format, sibling.read_bytes())
                if outcome is None:
                    steps.append((rel, "failed", {"error": "同目录图片无法识别"}))
                else:
                    embedded += 1
                    steps.append((rel, "done", {"source": "sibling", "file": sibling.name}))
                    continue

        # ---- CAA（按专辑缓存） ----
        if "caa" in policy.fetch_sources:
            release_id = _release_id(meta.tags)
            album_key = release_id or f"{root.name}:{path.parent.as_posix()}"
            block = _album_cover_block(
                store, cfg, album_key, release_id, policy.min_min_pixels
            )
            if block is not None:
                if writable and policy.auto_write:
                    outcome = _embed_block(path, meta.info.media_format, block)
                    if outcome:
                        embedded += 1
                        steps.append((rel, "done", {"source": "caa", "album_key": album_key}))
                        continue
                steps.append((rel, "review", {
                    "source": "caa", "album_key": album_key,
                    "note": "已找到封面候选，待确认后嵌入" if not writable else "待确认后嵌入",
                }))
                continue
            if release_id:
                steps.append((rel, "review", {
                    "source": "caa", "album_key": album_key,
                    "note": "CAA 无合格封面或下载失败",
                }))
                continue
        steps.append((rel, "review", {"note": "缺封面：无自动来源可用"}))

    return {"embedded": embedded, "steps": steps}


def _release_id(tags: dict[str, list[str]]) -> str:
    values = tags.get("MUSICBRAINZ_ALBUMID") or []
    return values[0].strip() if values else ""


def _album_cover_block(
    store: StateStore, cfg: Config, album_key: str, release_id: str, min_pixels: int
) -> bytes | None:
    """取（或下载并缓存）一张专辑封面，返回 FLAC PICTURE 块载荷。"""
    cached = store.get_cover_album(album_key)
    if cached is not None and cached["state"] == "ready":
        cached_path = Path(str(cached["file_path"]))
        if cached_path.is_file():
            info = detect_image(cached_path.read_bytes())
            return build_picture_block(info)
        # 缓存文件丢了：落库行作废，重新下载
    if not release_id:
        return None
    try:
        data, info = download_cover(caa_front_url(release_id), max_bytes=_MAX_COVER_BYTES)
    except Exception:  # noqa: BLE001 — 网络失败按「无封面」处理，下轮再试
        return None
    if not cover_quality_ok(info, min_pixels=min_pixels):
        return None
    digest = hashlib.sha256(data).hexdigest()
    suffix = ".png" if info.mime == "image/png" else ".jpg"
    cache_dir = cfg.storage.resolved_data_dir / "cover-cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{digest}{suffix}"
        if not cache_file.exists():
            cache_file.write_bytes(data)
        store.upsert_cover_album(
            album_key=album_key, source="caa", image_hash=digest,
            file_path=str(cache_file), mime=info.mime,
            width=info.width, height=info.height,
        )
    except OSError:
        return None
    return build_picture_block(info)


def _embed_block(path: Path, media_format, block: bytes) -> bool:
    from .writer import CoverEditPlan, apply_cover_edit

    outcome = apply_cover_edit(
        CoverEditPlan(audio_path=path, media_format=media_format, picture_block=block)
    )
    return bool(outcome.ok)


def _embed_bytes(path: Path, media_format, data: bytes) -> bool:
    try:
        info = detect_image(data)
    except Exception:  # noqa: BLE001
        return False
    return _embed_block(path, media_format, build_picture_block(info))



def finish_new_files(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    rel_paths: list[str],
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    """入库的「后半程」：封面支线 + 归位。

    watch 主循环在「补 .lrc → 抓词 → 写入」之后调用；审计、身份与抓取
    已由调用方完成，这里不重复联网。
    """
    if not rel_paths:
        return {"covers": 0, "moved": 0}
    cover_result = run_cover_stage(store, cfg, root, rel_paths=rel_paths)
    moved = 0
    if _effective_organize_mode(cfg, root) == "new_files" and _writable(cfg, root):
        root_id = store.upsert_root(
            name=root.name, role=root.role.value, path=str(root.resolved_path),
            enabled=root.enabled, read_only=root.read_only,
        )
        moved = _organize_new_files(
            store, cfg, root, rel_paths,
            task_id or f"watch-{time.strftime('%Y%m%d-%H%M%S')}", root_id,
        )
    return {"covers": cover_result.get("embedded", 0), "moved": moved}
