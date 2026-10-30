"""Web 应用：JSON API 与前端静态资源。

设计取舍：

- **API 优先。** 所有数据都走 `/api/v1/...` 的 JSON 接口，前端只是它的一个消费者。
  移动端直接复用这套接口，不需要后端改动。
- **鉴权不绑定 Cookie。** Web 用会话 Cookie，移动端/脚本用 `Authorization: Bearer`。
  两条路径都能独立工作。
- **默认只监听回环。** 由反向代理对外提供 HTTPS，不直接暴露端口。
- **长任务放后台线程。** 抓取/写入可能跑几分钟，不能卡住 HTTP 请求；
  同时写入天然必须串行，单工作线程正好满足。
- **页面与日志绝不输出凭据。**
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, flacmeta
from .auth import (
    AuthError,
    authenticate,
    create_token,
    ensure_admin,
    list_tokens,
    revoke_token,
    set_password,
    verify_token,
)
from .compare import build_comparison
from .config import (
    AI_PROTOCOLS,
    AI_SETTINGS_FIELDS,
    NETWORK_SETTINGS_FIELDS,
    Config,
    RootConfig,
    apply_acoustid_key,
    apply_ai_settings_values,
    apply_cover_policy_values,
    apply_lyrics_settings_values,
    apply_network_settings_values,
    apply_organize_policy_values,
    apply_tag_policy_values,
    read_ai_token,
)
from .config import (
    AI_TOKEN_FILENAME as _AI_TOKEN_FILENAME,
)
from .cover import (
    CoverDownloadError,
    CoverError,
    build_picture_block,
    detect_image,
    download_cover,
    parse_picture_block,
)
from .db import StateStore, open_store
from .fileops import move_to_trash
from .formats import (
    read_all_tags,
    read_audio_meta,
    read_embedded_lyrics_texts,
    sniff_container,
)
from .lrc import (
    content_hash,
    decode_lyrics_bytes,
    normalize_lrc,
    parse_lrc,
    parse_lrc_lines,
)
from .match import build_context, score_candidates
from .network import (
    PROXY_FEATURES,
    PROXY_SCHEMES,
    client_for,
    environment_proxy,
    load_network_settings,
    mask_proxy_url,
    proxy_bypass_hosts,
    proxy_reachable,
    set_active_network,
    write_saved_network,
)
from .providers.base import Candidate, ProviderError, TrackQuery
from .rename import apply_general_rename
from .stats import collect_stats
from .tagsources import (
    ACTION_CONFLICT,
    ACTION_CORRECT,
    ACTION_FILL,
    MbidLinkError,
    MusicBrainzConfig,
    MusicBrainzProvider,
    compare_fields,
    lookup_by_link,
    lookup_by_release,
    parse_entity_link,
    summarise,
)
from .writer import (
    CoverEditPlan,
    TagEditPlan,
    _fsync_dir,
    apply_cover_edit,
    apply_tag_edit,
    audio_hash_for,
)

__all__ = ["create_app", "Runner", "serve"]

logger = logging.getLogger(__name__)

_AUDIT_KEEP = 100
COOKIE_NAME = "lyra_session"
AI_TOKEN_FILENAME = _AI_TOKEN_FILENAME  # 兼容旧引用：常量已迁到 config


def _ai_token_path(cfg: Config) -> Path:
    return cfg.storage.resolved_data_dir / AI_TOKEN_FILENAME


def _read_ai_token(cfg: Config) -> str:
    """读取保存的 AI 令牌（实现已迁到 config，Web 与监控共用）。"""
    return read_ai_token(cfg)


def _write_ai_token(cfg: Config, token: str) -> None:
    path = _ai_token_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    path.chmod(0o600)


def _clear_ai_token(cfg: Config) -> None:
    with contextlib.suppress(OSError):
        _ai_token_path(cfg).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskRecord:
    id: str
    kind: str
    state: str = "running"  # running | done | failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


def _make_mb_provider(cfg: Config) -> MusicBrainzProvider:
    """按配置构造 MusicBrainz 客户端。调用方负责 `close()`。"""
    mb_cfg = cfg.providers.musicbrainz
    return MusicBrainzProvider(
        MusicBrainzConfig(
            base_url=mb_cfg.base_url,
            request_timeout_seconds=mb_cfg.request_timeout_seconds,
            minimum_request_interval_seconds=mb_cfg.minimum_request_interval_seconds,
            search_limit=mb_cfg.search_limit,
            detail_limit=mb_cfg.detail_limit,
        )
    )


def _recommended_fields(row) -> list[str]:
    """批量「应用建议」的字段集:除「一致」外的全部对比行。

    人工勾选了整行就等于确认按建议写,冲突也包含——与单条展开后
    默认全选非「一致」字段的口径一致。
    """
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") or []
    return [
        str(item.get("key"))
        for item in rows
        if isinstance(item, dict)
        and item.get("action") in (ACTION_FILL, ACTION_CORRECT, ACTION_CONFLICT)
        and str(item.get("key") or "").strip()
    ]


def _tag_proposal_dict(row, cfg: Config) -> dict[str, Any]:
    """提案行 → 接口返回结构。

    `payload_json` 里存的是完整对照（本地值、来源值、逐字段动作）；
    这里把它摊平，前端不必理解存储格式。
    """
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rows = payload.get("rows") or []
    track = payload.get("track") or {}
    fmt = str(track.get("format") or "").lower()
    root_name = str(row["root_name"])
    root = next((r for r in cfg.roots if r.name == root_name), None)
    writable = (
        fmt in ("flac", "mp3", "ogg_vorbis", "ogg_opus")
        and cfg.policy.write_enabled
        and (root is None or not root.read_only)
    )
    columns = row.keys()
    return {
        "id": int(row["id"]),
        "rel_path": str(row["rel_path"]),
        "root": str(row["root_name"]),
        "state": str(row["state"]),
        "dismissed_at": row["dismissed_at"] if "dismissed_at" in columns else None,
        "source": str(row["source"]),
        "source_label": (payload.get("match") or {}).get("source_label", ""),
        "confidence": float(row["confidence"] or 0),
        "album": str(row["album"] or ""),
        "title": str(row["title"] or ""),
        "artists": str(row["artists"] or ""),
        "duration_delta": row["duration_delta"],
        "track": {**track, "writable": writable},
        "rows": rows,
        "summary": payload.get("summary") or {},
        "extra": payload.get("extra") or {},
        "notes": payload.get("notes") or [],
        "candidates": payload.get("candidates") or [],
        # 格式能力以当前版本为准，不沿用旧提案里的布尔值：
        # 新支持 MP3 后，旧提案也应立即变得可写。
        "writable": writable,
        "match": payload.get("match") or {},
        "updated_at": float(row["updated_at"] or 0),
    }


def _organize_scope(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """解析整理范围：subpaths 可多选，空表示全库；兼容旧的单值 subpath。"""
    raw = payload.get("subpaths")
    if raw is None:
        legacy = str(payload.get("subpath") or "")
        subpaths = [legacy] if legacy else []
    elif isinstance(raw, list):
        subpaths = [str(item) for item in raw if str(item).strip()]
    else:
        raise HTTPException(status_code=400, detail="subpaths 必须是数组")
    raw_exclude = payload.get("exclude")
    if raw_exclude is None:
        exclude: list[str] = []
    elif isinstance(raw_exclude, list):
        exclude = [str(item) for item in raw_exclude if str(item).strip()]
    else:
        raise HTTPException(status_code=400, detail="exclude 必须是数组")
    return subpaths, exclude


def _organize_op_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "root": str(row["root_name"]),
        "batch_id": str(row["batch_id"]),
        "kind": str(row["op_kind"]),
        "old_rel_path": str(row["old_rel_path"]),
        "new_rel_path": str(row["new_rel_path"] or ""),
        "trash_rel_path": str(row["trash_rel_path"] or ""),
        "detail": row["detail"] or "",
        "created_at": float(row["created_at"] or 0),
        "undone_at": float(row["undone_at"]) if row["undone_at"] else None,
    }


def _record_from_row(row) -> TaskRecord:
    """把库里的任务行还原成 TaskRecord。"""
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = None
    return TaskRecord(
        id=row["id"], kind=row["kind"], state=row["state"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        progress=row["progress"] or "", result=result, error=row["error"],
    )


class Runner:
    """单工作线程的后台任务执行器。

    有意只用一个线程：写入必须串行（同一根只允许一个写入者），
    抓取也受来源限流约束。并发只会带来竞争，不会带来吞吐。
    """

    def __init__(self, store: StateStore | None = None) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._queue: list[tuple[str, Callable[[Callable[[str], None]], dict[str, Any]]]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 任务状态落库。不做的话重启容器就丢失全部任务记录——
        # 实际因此丢失过三次正在运行的任务，且用户看不到任何痕迹。
        self._store = store
        self._last_persist: dict[str, float] = {}

    def _persist(self, record: TaskRecord, *, force: bool = False) -> None:
        """把任务状态写进库。进度写入做节流——每个文件都写库会自己造成锁争用。"""
        if self._store is None:
            return
        now = time.time()
        if not force and now - self._last_persist.get(record.id, 0.0) < 2.0:
            return
        self._last_persist[record.id] = now
        attempts = 3 if force else 1
        for attempt in range(attempts):
            try:
                self._store.upsert_task(
                    task_id=record.id, kind=record.kind, state=record.state,
                    progress=record.progress, result=record.result, error=record.error,
                    started_at=record.started_at, finished_at=record.finished_at,
                )
                return
            except Exception as exc:  # noqa: BLE001 — 落库失败绝不能影响任务本身
                if attempt == attempts - 1:
                    logger.warning(
                        "任务 %s 状态落库失败（%s: %s）",
                        record.id, type(exc).__name__, exc,
                    )
                else:
                    time.sleep(0.2 * (attempt + 1))

    def start(
        self, kind: str, fn: Callable[[Callable[[str], None]], dict[str, Any]]
    ) -> TaskRecord:
        record = TaskRecord(id=uuid.uuid4().hex[:12], kind=kind)
        # 先落库再起线程。曾把「收尾落库」放在 spawn 之后：作业线程先跑完时，
        # 这条迟到的落库会把库里的 done 覆盖回 running、result 丢失——
        # 界面上任务永远转圈，重启后被标成 interrupted（实际发生过，约 1/4 概率）。
        self._persist(record, force=True)
        with self._lock:
            self._tasks[record.id] = record
            self._queue.append((record.id, fn))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        return record

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._queue:
                    return
                task_id, fn = self._queue.pop(0)
                record = self._tasks.get(task_id)
            if record is None:
                continue

            def progress(message: str, _rec: TaskRecord = record) -> None:
                _rec.progress = message
                self._persist(_rec)

            try:
                result = fn(progress)
            except Exception as exc:  # 后台任务不能把异常丢进日志黑洞
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
            else:
                record.state = "done"
                record.result = result
            finally:
                record.finished_at = time.time()
                self._persist(record, force=True)

    def get(self, task_id: str) -> TaskRecord | None:
        live = self._tasks.get(task_id)
        if live is not None:
            return live
        # 进程重启后内存里没有了，但库里还有——这样界面能显示
        # 「该任务被重启打断」而不是查无此任务
        if self._store is None:
            return None
        row = self._store.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list(self, limit: int = 20) -> list[TaskRecord]:
        if self._store is None:
            items = sorted(self._tasks.values(), key=lambda t: t.started_at, reverse=True)
            return items[:limit]
        merged: dict[str, TaskRecord] = {}
        for row in self._store.list_tasks(limit=limit * 2):
            record = _record_from_row(row)
            merged[record.id] = record
        # 内存中的实时进度比库里的新（入库有节流）
        for task_id, record in self._tasks.items():
            merged[task_id] = record
        items = sorted(merged.values(), key=lambda t: t.started_at, reverse=True)
        return items[:limit]

    def busy(self) -> bool:
        return any(t.state == "running" for t in self._tasks.values())


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


class _ImmutableStatic(StaticFiles):
    """构建产物的静态文件服务：文件名带哈希，可以标成永久缓存。"""

    async def get_response(self, path, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app(cfg: Config, *, store: StateStore | None = None) -> FastAPI:
    owned_store = store is None
    state = store or open_store(cfg.storage.resolved_data_dir)
    runner = Runner(state)
    web_dir = Path(__file__).resolve().parent / "web"

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        """启动与关闭时的一次性处理。

        用 lifespan 而不是 `@app.on_event(...)`：后者在 FastAPI 里已弃用，
        而本项目的 pytest 配置把弃用警告当作错误
        （`filterwarnings = ["error::DeprecationWarning"]`），
        所以任何会触发 startup 的测试（如 TestClient）都会直接失败。

        启动时做两件清理：
          - 把上个进程遗留的 `running` 任务标为被中断，界面才不会显示成还在跑；
          - 清掉遗留的写租约。租约有最长一小时的 TTL，进程被杀时来不及释放，
            新任务会一直被告知「已有任务在进行中」——实际遇到过这个坑。
        """
        try:
            # 设置页保存的代理优先；否则沿用配置文件/环境变量。
            load_network_settings(cfg)
            state.mark_interrupted_tasks()
            state.release_all_write_locks()
            # 进程重启后不可能还有人在等；残留的登记会让自动写入一直让出。
            state.clear_write_waits()
            # 设置页保存的标签策略优先于配置文件；启动时恢复。
            raw_policy = state.get_meta("settings.tags_policy")
            if raw_policy:
                apply_tag_policy_values(cfg, json.loads(raw_policy))
        except Exception:  # noqa: BLE001 — 清理失败不该阻止启动
            pass

        try:
            raw_ai = state.get_meta("settings.ai")
            if raw_ai:
                apply_ai_settings_values(cfg, json.loads(raw_ai))
            saved_token = _read_ai_token(cfg)
            if saved_token and not os.environ.get("LYRA_AI_API_KEY"):
                cfg.ai.api_key = saved_token
            env_key = os.environ.get("LYRA_AI_API_KEY")
            if env_key:
                cfg.ai.api_key = env_key
                cfg.ai.enabled = True
        except Exception as exc:  # noqa: BLE001 — 配置损坏不该阻止启动
            print(f"[startup] AI 设置读取失败：{exc}", flush=True)

        # 设置页保存的封面 / 归置 / 歌词输出 / 指纹 key 同样在启动时恢复。
        # 不恢复的话：用户开了「自动归位」，服务重启后归置模式悄悄回到 off，
        # 新文件不再归位，而设置页因为读内存也显示不出任何异常。
        for meta_key, apply_fn in (
            ("settings.cover_policy", apply_cover_policy_values),
            ("settings.organize_policy", apply_organize_policy_values),
            ("settings.lyrics_format", apply_lyrics_settings_values),
        ):
            try:
                raw = state.get_meta(meta_key)
                if raw:
                    apply_fn(cfg, json.loads(raw))
            except Exception as exc:  # noqa: BLE001 — 配置损坏不该阻止启动
                print(f"[startup] {meta_key} 读取失败：{exc}", flush=True)
        try:
            saved_acoustid = state.get_meta("settings.acoustid_key")
            if saved_acoustid and not os.environ.get("LYRA_ACOUSTID_API_KEY"):
                cfg.providers.acoustid.api_key = saved_acoustid
            if state.get_meta("settings.acoustid_enabled") is not None:
                cfg.providers.acoustid.enabled = (
                    state.get_meta("settings.acoustid_enabled") == "1"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] 指纹设置读取失败：{exc}", flush=True)

        # **统计预热**：后台线程每 3 分钟预计算一次曲库统计，
        # 让概览页总是命中缓存、秒开。数据最多滞后 3 分钟。
        import threading as _threading

        def _stats_refresher() -> None:
            while True:
                with contextlib.suppress(Exception):
                    _stats(cfg, refresh=True)
                _threading.Event().wait(180.0)

        _refresher = _threading.Thread(target=_stats_refresher, daemon=True)
        _refresher.start()

        # **崩溃恢复。** `recover_pending` 早就实现了、也有测试，但从来没有
        # 任何生产代码调用它——于是崩溃中断的事务永远停在非终态，
        # 界面上只显示「事务（未完成 N）」而无从处理。
        # 实际实际运行积了 6 个：写入其实都成功了，只是崩溃发生在
        # 「替换文件」与「提交事务记录」之间。
        try:
            from .writer import recover_pending

            recovered = recover_pending(state, dry_run=False)
            resolved = [i for i in recovered if i.action in ("completed", "already_rolled_back")]
            pending = [i for i in recovered if i.action not in ("completed", "already_rolled_back")]
            if resolved:
                print(f"[startup] 崩溃恢复：补记 {len(resolved)} 个已完成的事务", flush=True)
            if pending:
                # 无法自动判定的**必须说出来**，不能静默略过——
                # 那正是使用者需要知道「有文件需要人工确认」的时刻。
                #
                # 只报数量，**不要写死原因**：早先这里硬编码了
                # 「无可用备份材料」，而 needs_review 那类其实是有备份的，
                # 提示与实际不符会误导排查方向。
                reasons = sorted({i.action for i in pending})
                print(
                    f"[startup] 崩溃恢复：{len(pending)} 个事务需人工确认"
                    f"（{'/'.join(reasons)}），详见审计页",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 — 恢复失败不该阻止启动
            print(f"[startup] 崩溃恢复失败：{exc}", flush=True)
        # 凭据来自配置/环境变量；配置了就不生成随机密码、不写文件、不打印提示
        ensure_admin(
            state,
            cfg.storage.resolved_data_dir,
            username=cfg.web.admin_username,
            password=cfg.web.admin_password or None,
        )
        try:
            yield
        finally:
            if owned_store:
                state.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Lyra",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.cfg = cfg
    app.state.store = state
    app.state.runner = runner

    # -- 鉴权依赖 ---------------------------------------------------------

    def _token_from_request(request: Request) -> str | None:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.cookies.get(COOKIE_NAME)

    def require_auth(request: Request) -> str:
        if not cfg.web.auth_required:
            return "anonymous"
        token = _token_from_request(request)
        if not token or not verify_token(state, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token

    # -- 健康检查（无需鉴权，供反代/容器探活）-----------------------------

    # -- 图标 -------------------------------------------------------------

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        """站点图标。

        内联 SVG 而不是静态文件：只有一个 200 字节的资源，为它单独挂一个
        静态目录不值得，而且这样改了立刻生效、不会被浏览器缓存旧的二进制。

        Mark 是**天琴（lyre）**——Lyra 即天琴座，与项目同名；
        座中最亮的织女星（Vega）也与本项目的另一处命名呼应。
        """
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="7" fill="#11151c"/>'
            '<g fill="none" stroke="#7c9cff" stroke-width="2.4" '
            'stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M8 7c-3.5 8-1 16 8 20 9-4 11.5-12 8-20"/>'
            '<path d="M10.5 11.5h11"/>'
            '<path d="M13 11.5V24"/><path d="M16 11.5v14"/><path d="M19 11.5V24"/>'
            "</g></svg>"
        )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "schema_version": state.current_schema_version(),
            "write_enabled": cfg.policy.write_enabled,
            "busy": runner.busy(),
        }

    # -- 鉴权 -------------------------------------------------------------

    @app.post("/api/v1/auth/login")
    def login(request: Request, payload: dict[str, Any], response: Response) -> dict[str, Any]:
        username = str(payload.get("username") or "admin")
        password = str(payload.get("password") or "")
        if not authenticate(state, username, password):
            # 不区分「用户不存在」与「密码错误」
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = create_token(state, label=f"session:web:{username}", ttl_days=7)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            # **跟随实际协议。** 写死 secure=True 时，通过反代走 HTTPS 正常，
            # 但直接用 http://<内网地址>:端口 访问会登录成功却存不下 Cookie——
            # 下一次请求仍然 401，看起来像「密码对了却登不进去」。
            # 反代是否传 X-Forwarded-Proto 由 uvicorn 的 proxy_headers 处理。
            secure=(request.url.scheme == "https"),
            max_age=7 * 86400,
            path="/",
        )
        return {"ok": True, "username": username}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        token = _token_from_request(request)
        if token:
            row = state.find_token(_sha256(token))
            if row is not None:
                revoke_token(state, int(row["id"]))
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/v1/auth/me", dependencies=[Depends(require_auth)])
    def whoami(request: Request) -> dict[str, Any]:
        username = cfg.web.admin_username
        token = _token_from_request(request)
        if token:
            row = state.find_token(_sha256(token))
            label = str(row["label"] or "") if row is not None else ""
            if label.startswith("session:web:"):
                username = label.split(":", 2)[2]
        return {
            "ok": True,
            "auth_required": cfg.web.auth_required,
            "username": username,
        }

    @app.post("/api/v1/auth/token", dependencies=[Depends(require_auth)])
    def mint_token(payload: dict[str, Any]) -> dict[str, Any]:
        """签发 Bearer Token（供脚本与移动端使用）。明文只返回这一次。"""
        label = str(payload.get("label") or "api")
        ttl_days = payload.get("ttl_days", 365)
        token = create_token(
            state, label=label, ttl_days=int(ttl_days) if ttl_days else None
        )
        return {"ok": True, "token": token, "label": label}

    @app.get("/api/v1/tokens", dependencies=[Depends(require_auth)])
    def tokens() -> dict[str, Any]:
        return {"items": list_tokens(state)}

    @app.delete("/api/v1/tokens/{token_id}", dependencies=[Depends(require_auth)])
    def delete_token(token_id: int) -> dict[str, Any]:
        revoke_token(state, token_id)
        return {"ok": True}

    @app.post("/api/v1/auth/password", dependencies=[Depends(require_auth)])
    def change_password(payload: dict[str, Any]) -> dict[str, Any]:
        username = str(payload.get("username") or "admin")
        new = str(payload.get("new_password") or "")
        if len(new) < 10:
            raise HTTPException(status_code=400, detail="新密码至少 10 个字符")
        if not authenticate(state, username, str(payload.get("current_password") or "")):
            raise HTTPException(status_code=401, detail="当前密码错误")
        try:
            set_password(state, username, new)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    # -- 汇总 -------------------------------------------------------------

    @app.get("/api/v1/summary", dependencies=[Depends(require_auth)])
    def summary() -> dict[str, Any]:
        return _summary(cfg, state, runner)

    @app.get("/api/v1/tracks", dependencies=[Depends(require_auth)])
    def tracks(
        state_filter: str | None = Query(default=None, alias="state"),
        issue: str | None = None,
        q: str | None = None,
        page: int = 1,
        size: int = Query(default=50, le=500),
    ) -> dict[str, Any]:
        return _tracks(
            state, state_filter=state_filter, issue=issue, q=q, page=page, size=size
        )

    @app.get("/api/v1/issue-kinds", dependencies=[Depends(require_auth)])
    def issue_kinds() -> dict[str, Any]:
        """问题类型及各自数量，供音乐库页的筛选下拉使用。

        有意**由后端给出**而不是前端硬编码：新增问题码时下拉自动出现，
        不必两处同步（本项目已因「存储文案与代码不一致」遇到过三次）。
        """
        rows = state.conn.execute(
            "SELECT obs.issues FROM lyric_observations obs "
            "JOIN locations loc ON loc.id = obs.location_id "
            "WHERE loc.missing_since IS NULL "
            "AND obs.id IN (SELECT MAX(id) FROM lyric_observations GROUP BY location_id)"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            try:
                codes = json.loads(row["issues"] or "[]")
            except (TypeError, ValueError):
                continue
            for code in codes:
                counts[str(code)] = counts.get(str(code), 0) + 1
        kinds = [
            {"code": code, "count": n}
            for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {"kinds": kinds, "total": sum(counts.values())}

    # -- 文件管理（删除 / 替换）------------------------------------------

    def manual_lease(wait_seconds: float = 20.0):
        """给「人工点一下就改文件」的接口套上写租约。"""
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with _manual_lease(state, wait_seconds=wait_seconds,
                                   label="manual") as lease:
                    if not lease["acquired"]:
                        raise HTTPException(
                            status_code=409,
                            detail=f"写入任务仍在运行（{lease['holder']}），请稍后重试",
                        )
                    return fn(*args, **kwargs)
            return wrapper
        return deco


    @app.post("/api/v1/library/cover-names", dependencies=[Depends(require_auth)])
    @manual_lease()
    def normalize_cover_filenames(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """把封面图片名统一成小写（Cover.JPG → cover.jpg）。**只改大小写。**

        监控每小时自动做一次；这里是手动入口：`dry_run=true`（默认）只列计划，
        便于先看清会改哪些文件。同目录已有同名小写文件时，把非规范名的那份
        移入回收站（可还原），保留规范名的那份。
        """
        payload = payload or {}
        dry_run = payload.get("dry_run", True)
        if not dry_run and not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        from .pipeline import normalize_cover_names

        root_name = str(payload.get("root") or "")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        out = []
        for root_cfg in roots:
            if root_cfg.read_only and not dry_run:
                out.append({"root": root_cfg.name, "skipped_read_only": True})
                continue
            out.append({"root": root_cfg.name,
                        **normalize_cover_names(state, cfg, root_cfg, dry_run=bool(dry_run))})
        if not dry_run:
            _invalidate_caches()
        return {"roots": out, "dry_run": bool(dry_run)}

    @app.post("/api/v1/files/delete", dependencies=[Depends(require_auth)])
    @manual_lease()
    def delete_file(payload: dict[str, Any]) -> dict[str, Any]:
        """把文件或目录**移动**到数据目录的回收站，而不是直接抹掉。

        为什么用移动而不是 unlink：这些都是用户的音乐，误删无法挽回；
        而同一卷内 rename 是瞬时的，既不占额外空间也不耗时。
        真要腾空间时手动清理 `data/trash/` 即可。
        """
        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        if not root_name or not rel:
            raise HTTPException(status_code=400, detail="必须提供 root 与 path")
        root = _select_root(cfg, root_name)
        target = _resolve_within_root(root, rel)
        if target is None:
            raise HTTPException(status_code=404, detail="不存在")
        if target == root.resolved_path:
            raise HTTPException(status_code=400, detail="不允许删除受管根本身")

        batch = time.strftime("%Y%m%d-%H%M%S")
        moved = _move_to_trash(cfg, root, target, batch)
        moved_sidecar = None
        # 音频被删掉后，同名 .lrc 就成了孤儿；一并移走更干净
        if _kind_of(target) == "audio":
            sidecar = target.with_suffix(".lrc")
            if sidecar.is_file():
                moved_sidecar = _move_to_trash(cfg, root, sidecar, batch)
        _invalidate_caches()
        return {
            "ok": True, "moved_to": moved, "sidecar_moved_to": moved_sidecar,
            "trash_dir": str(cfg.storage.resolved_data_dir / "trash"),
            "batch": batch,
        }

    @app.post("/api/v1/files/batch-delete", dependencies=[Depends(require_auth)])
    def batch_delete_files(payload: dict[str, Any]) -> dict[str, Any]:
        """把多个文件/目录移入回收站（同一个批次，便于整批还原）。

        走与单个删除完全相同的 `_move_to_trash`：保留相对路径、可还原。
        单个失败不影响其它项——批量操作最忌讳「第 3 个失败就整批不动」。
        """
        root_name = str(payload.get("root") or "")
        raw_paths = payload.get("paths") or []
        if not root_name or not isinstance(raw_paths, list) or not raw_paths:
            raise HTTPException(status_code=400, detail="必须提供 root 与 paths")
        root = _select_root(cfg, root_name)
        batch = time.strftime("%Y%m%d-%H%M%S")
        moved: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        with _manual_lease(state, wait_seconds=20, label="manual-delete") as lease:
            if not lease["acquired"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"写入任务仍在运行（{lease['holder']}），请稍后重试",
                )
            for rel in raw_paths:
                rel = str(rel)
                target = _resolve_within_root(root, rel)
                if target is None:
                    skipped.append(rel)
                    continue
                if target == root.resolved_path:
                    errors.append(f"{rel}: 不允许删除受管根本身")
                    continue
                try:
                    _move_to_trash(cfg, root, target, batch)
                    moved.append(rel)
                    if _kind_of(target) == "audio":
                        sidecar = target.with_suffix(".lrc")
                        if sidecar.is_file():
                            _move_to_trash(cfg, root, sidecar, batch)
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
        _invalidate_caches()
        return {"ok": True, "moved": moved, "skipped": skipped,
                "errors": errors, "batch": batch,
                "trash_dir": str(cfg.storage.resolved_data_dir / "trash")}

    @app.get("/api/v1/trash", dependencies=[Depends(require_auth)])
    def list_trash() -> dict[str, Any]:
        """列出回收站内容，按批次分组。

        删除是**移动**而不是抹除，所以必须能看、能还原、能清空——
        否则「移入回收站」只是一句空话。
        """
        trash = cfg.storage.resolved_data_dir / "trash"
        batches: list[dict[str, Any]] = []
        total = 0
        total_bytes = 0
        if trash.is_dir():
            for batch in sorted(trash.iterdir(), reverse=True):
                if not batch.is_dir():
                    continue
                items = []
                for path in sorted(batch.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = str(path.relative_to(batch))
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                    items.append({
                        "rel_path": rel,
                        "name": path.name,
                        "size": size,
                        "kind": _kind_of(path),
                    })
                    total_bytes += size
                if items:
                    batches.append({"batch": batch.name, "items": items})
                    total += len(items)
        return {"total": total, "bytes": total_bytes, "batches": batches}

    @app.post("/api/v1/trash/restore", dependencies=[Depends(require_auth)])
    def restore_trash(payload: dict[str, Any]) -> dict[str, Any]:
        """把回收站里的文件放回原位。

        目标位置若已被占用则**拒绝**而不是覆盖——那可能是有意放进去的新文件。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        batch = str(payload.get("batch") or "")
        rel = str(payload.get("path") or "")
        root_name = str(payload.get("root") or "")
        if not batch or not rel or not root_name:
            raise HTTPException(status_code=400, detail="必须提供 batch、path 与 root")
        root = _select_root(cfg, root_name)
        trash = cfg.storage.resolved_data_dir / "trash"
        source = (trash / batch / rel).resolve()
        if not str(source).startswith(str(trash.resolve())) or not source.is_file():
            raise HTTPException(status_code=404, detail="回收站里找不到该文件")
        target = root.resolved_path / rel
        if target.exists():
            raise HTTPException(
                status_code=409, detail=f"原位置已有同名文件，拒绝覆盖：{rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        _fsync_dir(target.parent)

        # 删除时音频与同名 .lrc 是一起移走的，还原也要一起——
        # 只还音频会让歌词留在回收站里变成孤儿，而用户以为自己「还原了」。
        restored_extra: list[str] = []
        if Path(rel).suffix.lower() != ".lrc":
            sidecar_rel = str(Path(rel).with_suffix(".lrc"))
            sidecar_src = trash / batch / sidecar_rel
            sidecar_dst = root.resolved_path / sidecar_rel
            if sidecar_src.is_file() and not sidecar_dst.exists():
                sidecar_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(sidecar_src), str(sidecar_dst))
                _fsync_dir(sidecar_dst.parent)
                restored_extra.append(sidecar_rel)
        # 批次空了就顺手清掉空目录
        parent = source.parent
        while parent != trash and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        _invalidate_caches()
        return {"ok": True, "restored_to": rel, "also_restored": restored_extra}

    @app.post("/api/v1/trash/empty", dependencies=[Depends(require_auth)])
    def empty_trash(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """**永久删除**回收站内容。不可恢复，所以必须显式确认。"""
        payload = payload or {}
        batch = str(payload.get("batch") or "")
        if payload.get("confirm") is not True:
            raise HTTPException(
                status_code=400,
                detail="永久删除不可恢复，必须在请求里显式传入 confirm=true",
            )
        trash = cfg.storage.resolved_data_dir / "trash"
        removed_files = 0
        removed_bytes = 0
        if trash.is_dir():
            targets = [trash / batch] if batch else list(trash.iterdir())
            for item in targets:
                if not item.exists():
                    continue
                for path in item.rglob("*") if item.is_dir() else [item]:
                    if path.is_file():
                        removed_files += 1
                        with contextlib.suppress(OSError):
                            removed_bytes += path.stat().st_size
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        _invalidate_caches()
        return {"ok": True, "removed_files": removed_files, "removed_bytes": removed_bytes}

    @app.post("/api/v1/files/replace", dependencies=[Depends(require_auth)])
    async def replace_file(
        request: Request,
        root: str = Query(...),
        path: str = Query(...),
        move_old: bool = Query(default=True),
    ) -> dict[str, Any]:
        """用上传的文件**替换**目标音频。

        关键行为：文件名以**目标**为准，扩展名以**上传内容的真实容器**为准。
        实际用户会拿一个 mp3 去替换一个错误的 flac——正确结果是
        `abc.flac` → `abc.mp3`（保留曲名，改随真实格式），
        而不是保留 `.flac` 这个骗人的后缀。

        上传体用原始字节而不是 multipart：省掉 python-multipart 依赖，
        前端直接 `fetch(url, {body: file})` 即可。
        """
        root_cfg = _select_root(cfg, root)
        target = _resolve_within_root(root_cfg, path)
        if target is None:
            raise HTTPException(status_code=404, detail="目标文件不存在")

        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="上传内容为空")
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
            )

        # 先落到目标同目录的临时文件，再按真实容器判定最终文件名
        tmp = target.with_name(f".lyra-upload-{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

            media_format = sniff_container(tmp)
            if not media_format.supported:
                raise HTTPException(
                    status_code=415,
                    detail="无法识别上传文件的音频容器（支持 FLAC / OGG / Opus / MP3）",
                )
            new_ext = media_format.canonical_extension
            final = target.with_suffix(new_ext) if target.suffix.lower() != new_ext else target

            # 目标名若已被别的文件占用（例如 abc.flac 与 abc.mp3 并存），先移走
            if final != target and final.exists():
                trash = cfg.storage.resolved_data_dir / "replaced"
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(final), str(trash / f"{time.strftime('%Y%m%d-%H%M%S')}__{final.name}"))

            old_moved = None
            if move_old:
                trash = cfg.storage.resolved_data_dir / "replaced"
                trash.mkdir(parents=True, exist_ok=True)
                dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}__{target.name}"
                counter = 1
                while dest.exists():
                    dest = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{counter}__{target.name}"
                    counter += 1
                shutil.move(str(target), str(dest))
                old_moved = str(dest)

            os.replace(tmp, final)
            _fsync_dir(final.parent)
        except BaseException:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

        _invalidate_caches()
        return {
            "ok": True,
            "target": path,
            "final_name": final.name,
            "final_rel_path": str(final.relative_to(root_cfg.resolved_path)),
            "renamed": final.name != target.name,
            "detected_format": media_format.value,
            "extension_used": new_ext,
            "old_moved_to": old_moved,
            "bytes": len(data),
        }

    def _refresh_observation(root_cfg: RootConfig, rel_path: str) -> bool:
        """写入后立刻重读这个文件的观察结果，返回是否刷新成功。

        不做这一步会有一个很难解释的现象：点了「写入」、提示成功，但问题徽章
        仍然挂在页面上，直到监控的周期对账（30 秒）跑过才消失——实际使用者
        会直接认为「写了但没生效」，然后重试、重试、再重试。

        只重读**这一个文件**，不做全库审计：写入接口是同步阻塞的 HTTP 请求，
        全库审计会让一次保存等上几十秒。`full_scan=False` 保证不会顺手把
        别的路径标成缺失。
        """
        from .audit import audit_root
        from .pipeline import sync_state

        try:
            refreshed = audit_root(root_cfg, only_rel_paths={rel_path})
            sync_state(state, cfg, root_cfg, refreshed)
            return True
        except Exception:  # noqa: BLE001 — 刷新失败不该让写入看起来失败
            return False

    def _store_cover(
        root_cfg: RootConfig,
        resolved: Path,
        rel_path: str,
        *,
        picture_block: bytes | None,
        cover_info: dict[str, Any],
        removed: bool,
        source_url: str | None = None,
        source_file: str | None = None,
    ) -> dict[str, Any]:
        """把已经构造好的封面块写进文件并刷新观察。

        上传、链接下载两条入口共用它：两条路**只允许在「图片来源」上不同**，
        落到磁盘上的动作必须一模一样（备份语义、音频哈希校验、状态刷新）。
        """
        meta = read_audio_meta(resolved, root_cfg.resolved_path)
        outcome = apply_cover_edit(
            CoverEditPlan(
                audio_path=resolved,
                media_format=meta.info.media_format,
                picture_block=picture_block,
            )
        )
        if not outcome.ok:
            raise HTTPException(status_code=422, detail=outcome.error)
        _invalidate_caches()
        refreshed = _refresh_observation(root_cfg, rel_path)
        return {
            "ok": True, "rel_path": rel_path, "removed": removed,
            "has_cover": outcome.has_cover, "cover": cover_info or None,
            "source_url": source_url, "source_file": source_file,
            "audio_hash": outcome.audio_hash, "full_hash": outcome.full_hash,
            "state_refreshed": refreshed,
        }

    @app.post("/api/v1/files/tags", dependencies=[Depends(require_auth)])
    def write_tags(payload: dict[str, Any]) -> dict[str, Any]:
        """改写音频文件的标签。**音频数据保证逐字节不变。**

        `tags` 里的字段被整体替换，`remove` 里的被删除，其余字段按原字节、
        原顺序保留；封面不受影响。返回结果里带改写前后的音频哈希，
        让调用方能自己核对「音频真的没被动过」。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        if not root_name or not rel:
            raise HTTPException(status_code=400, detail="必须提供 root 与 path")

        raw_tags = payload.get("tags") or {}
        raw_remove = payload.get("remove") or []
        if not isinstance(raw_tags, dict) or not isinstance(raw_remove, list):
            raise HTTPException(status_code=400, detail="tags 必须是对象，remove 必须是数组")

        tags: dict[str, list[str]] = {}
        for key, values in raw_tags.items():
            name = _validated_tag_key(key)
            if not isinstance(values, list):
                values = [values]
            cleaned = [str(v) for v in values]
            if not cleaned:
                # 空数组等价于删除该字段，比让前端自己判断更不容易出错
                raw_remove = [*raw_remove, name]
                continue
            for value in cleaned:
                if "\x00" in value:
                    raise HTTPException(status_code=400, detail=f"{name} 含非法字符")
            tags[name] = cleaned
        remove = [_validated_tag_key(k) for k in raw_remove]

        if not tags and not remove:
            raise HTTPException(status_code=400, detail="没有要修改的标签")

        root_cfg, resolved = _safe_root_file(cfg, root_name, rel)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件支持标签改写")
        meta = read_audio_meta(resolved, root_cfg.resolved_path)

        outcome = apply_tag_edit(
            TagEditPlan(
                audio_path=resolved,
                media_format=meta.info.media_format,
                tags=tags,
                remove=remove,
                original_audio_hash=audio_hash_for(resolved, meta.info.media_format) or "",
            )
        )
        if not outcome.ok:
            raise HTTPException(status_code=422, detail=outcome.error)
        _invalidate_caches()
        refreshed = _refresh_observation(root_cfg, rel)
        return {
            "ok": True,
            "rel_path": rel,
            "applied": outcome.applied,
            "removed": outcome.removed,
            "audio_hash": outcome.audio_hash,
            "full_hash": outcome.full_hash,
            "state_refreshed": refreshed,
            "all_tags": read_all_tags(resolved)[0],
        }

    @app.post("/api/v1/files/rename", dependencies=[Depends(require_auth)])
    @manual_lease()
    def rename_file(payload: dict[str, Any]) -> dict[str, Any]:
        """给任意文件改名（不限音频类型），并同步改名配对的 .lrc。

        只支持同目录内改名——跨目录需要移动，无法保证原子性。
        播放列表里的引用会在改名后失效，因此**提前返回警告**而不是改完才说。
        """
        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        new_name = str(payload.get("new_name") or "")
        if not root_name or not rel or not new_name:
            raise HTTPException(status_code=400, detail="必须提供 root、path 与 new_name")

        root_cfg = _select_root(cfg, root_name)
        source = _resolve_within_root(root_cfg, rel)
        if source is None:
            raise HTTPException(status_code=404, detail="文件不存在")

        root_row = state.conn.execute(
            "SELECT id FROM roots WHERE name=?", (root_cfg.name,)
        ).fetchone()
        result = apply_general_rename(
            state, root_cfg,
            source=source,
            new_name=new_name,
            root_id=int(root_row["id"]) if root_row else None,
        )
        if not result.ok:
            raise HTTPException(status_code=422, detail=result.error)
        _invalidate_caches()
        return {
            "ok": True,
            "old_rel_path": result.old_rel_path,
            "new_rel_path": result.new_rel_path,
            "sidecar_renamed": result.sidecar_renamed,
            "rename_id": result.rename_id,
            "warnings": result.warnings,
        }

    @app.post("/api/v1/files/cover", dependencies=[Depends(require_auth)])
    @manual_lease()
    async def write_cover(
        request: Request,
        root: str = Query(...),
        path: str = Query(...),
        remove: bool = Query(default=False),
    ) -> dict[str, Any]:
        """替换或移除音频文件的封面。

        图片用**原始字节**作为请求体（与替换文件同一套做法，省掉 multipart
        依赖，前端 `fetch(url, {body: file})` 即可）。
        `remove=true` 时忽略请求体，直接移除封面。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件支持封面写入")

        picture_block: bytes | None = None
        info: dict[str, Any] = {}
        if not remove:
            data = await request.body()
            if not data:
                raise HTTPException(status_code=400, detail="上传内容为空")
            if len(data) > _MAX_COVER_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"封面过大（上限 {_MAX_COVER_BYTES // (1024 * 1024)} MB）",
                )
            try:
                cover = detect_image(data)
            except CoverError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc
            picture_block = build_picture_block(cover)
            info = {
                "mime": cover.mime, "width": cover.width,
                "height": cover.height, "bytes": len(cover.data),
            }

        return _store_cover(
            root_cfg, resolved, path,
            picture_block=picture_block, cover_info=info, removed=remove,
        )

    @app.get("/api/v1/files/cover/sibling", dependencies=[Depends(require_auth)])
    def sibling_cover(root: str, path: str) -> dict[str, Any]:
        """查询音频所在目录里有没有可用的封面文件（`cover.jpg` 等）。

        界面上「用同目录封面」这个按钮只有在**确实存在**时才出现：
        库里不少单曲/EP 根本没有 cover 文件，摆一个点了没反应的按钮
        比没有这个功能更糟。
        """
        _root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件有封面")

        hit = _find_sibling_cover(resolved)
        if hit is None:
            return {"found": False}
        try:
            data = hit.read_bytes()
            info = detect_image(data)
        except (OSError, CoverError) as exc:
            return {"found": False, "error": f"同目录的 {hit.name} 读不出来：{exc}"}
        return {
            "found": True, "name": hit.name, "bytes": len(data),
            "mime": info.mime, "width": info.width, "height": info.height,
        }

    @app.post("/api/v1/files/cover/from-dir", dependencies=[Depends(require_auth)])
    @manual_lease()
    def write_cover_from_dir(payload: dict[str, Any]) -> dict[str, Any]:
        """把同目录里的封面图片嵌入音频文件。

        来源是**本地文件**，不联网；读取与写入都与「上传」走同一个函数，
        所以备份语义、音频哈希校验、状态刷新完全一致。

        找不到封面文件时返回 404 并说明「找过哪些名字」——
        只说「没有封面」会让人怀疑是路径写错了。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        if not root_name or not rel:
            raise HTTPException(status_code=400, detail="必须提供 root 与 path")

        root_cfg, resolved = _safe_root_file(cfg, root_name, rel)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件支持封面写入")

        hit = _find_sibling_cover(resolved)
        if hit is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "这个目录里没有可用的封面文件。找过："
                    + "、".join(_SIBLING_COVER_NAMES[:6])
                    + " 等常见命名，以及以 cover/folder/front/album 开头的图片。"
                ),
            )
        try:
            data = hit.read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=422, detail=f"读取 {hit.name} 失败：{exc}"
            ) from exc
        if len(data) > _MAX_COVER_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"封面过大（上限 {_MAX_COVER_BYTES // (1024 * 1024)} MB）",
            )
        try:
            cover = detect_image(data)
        except CoverError as exc:
            raise HTTPException(
                status_code=415, detail=f"{hit.name} 不是可识别的图片：{exc}"
            ) from exc

        return _store_cover(
            root_cfg, resolved, rel,
            picture_block=build_picture_block(cover),
            cover_info={
                "mime": cover.mime, "width": cover.width,
                "height": cover.height, "bytes": len(cover.data),
            },
            removed=False,
            source_file=hit.name,
        )

    @app.post("/api/v1/files/cover/from-url", dependencies=[Depends(require_auth)])
    @manual_lease()
    def write_cover_from_url(payload: dict[str, Any]) -> dict[str, Any]:
        """按链接下载封面并写入内嵌封面。

        为什么由服务端下载：这些图片源（`y.gtimg.cn`、`p1.music.126.net`）
        大多不带 CORS 头，浏览器里 `<img>` 能显示但 `fetch` 拿不到字节，
        前端没法把图转成上传内容。让服务端抓一次是最直接的路子。

        **同时是 SSRF 边界**：只允许 http/https，且域名必须解析到公网地址
        （见 `cover.ensure_public_host`）。否则任何登录用户都能借它探测内网。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        url = str(payload.get("url") or "").strip()
        if not root_name or not rel:
            raise HTTPException(status_code=400, detail="必须提供 root 与 path")
        if not url:
            raise HTTPException(status_code=400, detail="必须提供图片链接")

        root_cfg, resolved = _safe_root_file(cfg, root_name, rel)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件支持封面写入")

        try:
            data, cover = download_cover(url, max_bytes=_MAX_COVER_BYTES)
        except CoverDownloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return _store_cover(
            root_cfg, resolved, rel,
            picture_block=build_picture_block(cover),
            cover_info={
                "mime": cover.mime, "width": cover.width,
                "height": cover.height, "bytes": len(cover.data),
            },
            removed=False,
            source_url=url,
        )

    @app.get("/api/v1/files/cover", dependencies=[Depends(require_auth)])
    def read_cover(root: str, path: str) -> Response:
        """取出内嵌封面图片，直接返回图片字节供 <img> 显示。"""
        _root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件有内嵌封面")
        try:
            layout = flacmeta.flac_parse_file(resolved)
        except Exception as exc:  # noqa: BLE001 — 解析失败如实上报
            raise HTTPException(status_code=422, detail=f"解析失败: {exc}") from exc
        payload = layout.block(flacmeta.BlockType.PICTURE)
        if payload is None:
            raise HTTPException(status_code=404, detail="该文件没有内嵌封面")
        try:
            mime, data = parse_picture_block(payload)
        except CoverError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(content=data, media_type=mime or "image/jpeg")

    @app.get("/api/v1/files/sidecar", dependencies=[Depends(require_auth)])
    def file_sidecar(root: str, path: str) -> dict[str, Any]:
        """这一个音频**实际配对到**的 .lrc 是哪个、名字是否与音频不同。

        界面上的「异名配对」修复面板需要真实文件名：音频叫 `01 童年時.flac`，
        它可能配到 `01 - 张国荣 - 童年时.lrc`——只按 `<音频同名>.lrc` 猜，
        预览和改名都会落到一个不存在的路径上。
        """
        from .audit import audit_root
        from .models import IssueCode

        root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件有歌词配对")
        audit = audit_root(root_cfg, only_rel_paths={path})
        track = next(
            (t for t in audit.tracks if str(t.audio.rel_path) == path), None
        )
        if track is None:
            raise HTTPException(status_code=404, detail="文件不存在或不在受管根内")
        sidecar = track.sidecar
        # 审计走的是 resolve 过的绝对路径（macOS 上 /tmp 是 /private/tmp 的
        # 软链），这里必须用同样 resolve 过的根做相对化，否则会报
        # 「not in the subpath」——真实环境里用户配置带软链路径同样会踩到
        base = root_cfg.resolved_path.resolve()
        return {
            "path": (
                str(sidecar.path.resolve().relative_to(base))
                if sidecar.exists and sidecar.path is not None else None
            ),
            "exists": bool(sidecar.exists),
            "state": sidecar.state.value,
            "name_differs": IssueCode.SIDECAR_NAME_MISMATCH in track.issues,
            "wanted": str(resolved.with_suffix(".lrc").relative_to(base)),
        }

    @app.get("/api/v1/files/tags/suggest", dependencies=[Depends(require_auth)])
    def suggest_tags(root: str, path: str, release: str = "") -> dict[str, Any]:
        """从 MusicBrainz 取回标签建议，与当前标签做**字段级**对比。

        只读：不写入任何文件。返回每个候选的逐字段对比与建议动作
        （`conflict` 需人工确认 / `fill` 可直接填充 / `same` 无需动作）。
        """
        mb_cfg = cfg.providers.musicbrainz
        if not mb_cfg.enabled or not mb_cfg.base_url.strip():
            raise HTTPException(
                status_code=503,
                detail="未配置 MusicBrainz（providers.musicbrainz.base_url）",
            )
        root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件有标签")

        meta = read_audio_meta(resolved, root_cfg.resolved_path)
        all_tags, tags_error = read_all_tags(resolved)
        if tags_error:
            raise HTTPException(status_code=422, detail=tags_error)

        context = build_context(meta.tags, meta.info.duration)
        provider = MusicBrainzProvider(
            MusicBrainzConfig(
                base_url=mb_cfg.base_url,
                request_timeout_seconds=mb_cfg.request_timeout_seconds,
                minimum_request_interval_seconds=mb_cfg.minimum_request_interval_seconds,
                search_limit=mb_cfg.search_limit,
                detail_limit=mb_cfg.detail_limit,
            )
        )
        # **优先按文件里已有的 MBID 精确查询。**
        # 曲库 97% 的文件被 Picard 写过 MBID，直接拉它原来那张发行即可，
        # 结果唯一且精确。重新搜索返回的是「这首录音还出现在哪些发行上」，
        # 往往不是文件原本所属的那张——实际会把 ALBUM/DATE 全报成假冲突。
        exact: list = []
        album_id = str((all_tags.get("MUSICBRAINZ_ALBUMID") or [""])[0])
        if album_id.strip():
            try:
                hit = lookup_by_release(
                    provider,
                    release_id=album_id,
                    recording_id=str((all_tags.get("MUSICBRAINZ_TRACKID") or [""])[0]),
                    release_track_id=str(
                        (all_tags.get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
                    ),
                    # 有 ALBUMID 但缺 TRACKID 的文件（例如只被写了一半标签）：
                    # 靠标题 + 时长在同一张发行里定位，否则整条曲序都填不上。
                    title=context.title,
                    duration=context.duration,
                )
            except httpx.HTTPError:
                hit = None
            if hit is not None:
                exact.append(hit)

        try:
            # 没有 MBID（或按 MBID 查不到）才退回搜索
            searched = provider.lookup(
                title=context.title,
                artists=context.artists,
                release=release.strip() or context.album,
                duration=context.duration,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"MusicBrainz 请求失败：{exc}"
            ) from exc
        finally:
            provider.close()

        candidates = exact + [c for c in searched if c.provider_id not in {e.provider_id for e in exact}]

        out = []
        for cand in candidates[:5]:
            # 时长差是这里的主力判据：实际 MusicBrainz 自己的 score 对
            # 现场版与录音室版都给满分，只有时长能把它们分开。
            delta = None
            if cand.duration is not None and context.duration is not None:
                delta = abs(cand.duration - context.duration)
            rows = compare_fields(all_tags, cand.fields)
            is_exact = bool(exact) and any(cand is e for e in exact)
            out.append({
                **cand.to_dict(),
                "exact": is_exact,
                "source_kind": "按 MBID 精确查询" if is_exact else "标题艺人搜索",
                "duration_delta": delta,
                "rows": rows,
                "summary": summarise(rows),
            })

        return {
            "rel_path": path,
            "track": {
                "title": context.title,
                "artists": list(context.artists),
                "album": context.album,
                "duration": context.duration,
            },
            "source": {"provider": "musicbrainz", "base_url": mb_cfg.base_url},
            "candidates": out,
        }

    @app.get("/api/v1/files/tags/link", dependencies=[Depends(require_auth)])
    def lookup_tags_by_link(root: str, path: str, url: str) -> dict[str, Any]:
        """按一条 MusicBrainz 链接取回该文件可写入的字段。**只读。**

        「缺 MusicBrainz ID」这个缺陷过去只能靠人手工抄 UUID，既痛苦又容易
        写错（实际就写错过：ALBUMID 抄成了单曲那张，而文件属于合辑）。
        这条路径把「我确定的这一条」变成输入：链接 → 逐字段对比 → 人工勾选。

        链接里的 MBID 直接决定查询对象，**不经过相似度匹配**——使用者
        点的是哪一个条目就是哪一个，系统只负责把字段取全、把与本地不一致
        的地方标出来。
        """
        mb_cfg = cfg.providers.musicbrainz
        if not mb_cfg.enabled or not mb_cfg.base_url.strip():
            raise HTTPException(
                status_code=503,
                detail="未配置 MusicBrainz（providers.musicbrainz.base_url）",
            )
        root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频文件有标签")

        try:
            link = parse_entity_link(url)
        except MbidLinkError as exc:
            # 用户输入问题 → 400，并把「怎么改」写清楚，别只说「无效」
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        meta = read_audio_meta(resolved, root_cfg.resolved_path)
        all_tags, tags_error = read_all_tags(resolved)
        if tags_error:
            raise HTTPException(status_code=422, detail=tags_error)
        context = build_context(meta.tags, meta.info.duration)

        provider = MusicBrainzProvider(
            MusicBrainzConfig(
                base_url=mb_cfg.base_url,
                request_timeout_seconds=mb_cfg.request_timeout_seconds,
                minimum_request_interval_seconds=mb_cfg.minimum_request_interval_seconds,
                search_limit=mb_cfg.search_limit,
                detail_limit=mb_cfg.detail_limit,
            )
        )
        try:
            candidates = lookup_by_link(
                provider,
                link,
                # 本地这些值只用于**排序与标注**（哪张发行更像本文件所属的），
                # 不参与「要不要采用」，采用与否由人勾选。
                title=context.title,
                duration=context.duration,
                album=context.album,
                album_id=str((all_tags.get("MUSICBRAINZ_ALBUMID") or [""])[0]),
                recording_id=str((all_tags.get("MUSICBRAINZ_TRACKID") or [""])[0]),
                release_track_id=str(
                    (all_tags.get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
                ),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"MusicBrainz 请求失败：{exc}"
            ) from exc
        finally:
            provider.close()

        out = []
        for index, cand in enumerate(candidates[:8]):
            delta = None
            if cand.duration is not None and context.duration is not None:
                delta = abs(cand.duration - context.duration)
            rows = compare_fields(all_tags, cand.fields)
            out.append({
                **cand.to_dict(),
                "exact": True,
                "rank": index + 1,
                "from_link": True,
                "source_kind": f"链接直查（{link.to_dict()['entity_label']}）",
                "duration_delta": delta,
                "rows": rows,
                "summary": summarise(rows),
            })

        return {
            "rel_path": path,
            "link": link.to_dict(),
            "track": {
                "title": context.title,
                "artists": list(context.artists),
                "album": context.album,
                "duration": context.duration,
            },
            "source": {"provider": "musicbrainz", "base_url": mb_cfg.base_url},
            "candidates": out,
        }

    @app.get("/api/v1/staged", dependencies=[Depends(require_auth)])
    def staged(
        state_filter: str | None = Query(default=None, alias="state"),
        page: int = 1,
        size: int = Query(default=50, le=500),
        by_track: bool = False,
        exclude_safe: bool = False,
    ) -> dict[str, Any]:
        return _staged(
            state, state_filter=state_filter, page=page, size=size,
            by_track=by_track, exclude_safe=exclude_safe,
            provider_order=list(cfg.providers.enabled),
        )

    @app.get("/api/v1/staged/safe-batch", dependencies=[Depends(require_auth)])
    def staged_safe_batch() -> dict[str, Any]:
        """列出**硬闸门全过、仅软门槛未达**的待审查项。

        这一批是真正「可以放心接受」的：时长、版本、标题、艺人四道硬闸门
        全部通过，说明来源返回的就是这首歌；只是加权总分没到自动接受线。
        雷同度低、风险小——与「来源返回了另一首歌」那类是本质不同的。
        """
        safe = _safe_batch_entries(state)
        return {"total": len(safe), "items": safe}

    @app.post("/api/v1/staged/safe-batch/accept", dependencies=[Depends(require_auth)])
    def staged_safe_batch_accept(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """**显式动作**：接受所有硬闸门全过、仅软门槛未达的项。

        有意做成按钮而不是自动行为——放宽接受标准是**策略决定**，
        该由使用者按下，不该藏在流程里悄悄发生。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        payload = payload or {}
        ids = payload.get("ids")
        listed = staged_safe_batch()
        targets = [
            it for it in listed["items"]
            if not ids or it["id"] in set(ids)
        ]
        accepted = 0
        for item in targets:
            try:
                _set_staged(state, int(item["id"]), "staged")
                accepted += 1
            except Exception as exc:  # noqa: BLE001 — 单条失败不影响其余
                logger.warning("批量接受失败 id=%s: %s", item["id"], exc)
        _invalidate_caches()
        return {"ok": True, "accepted": accepted, "total": len(targets)}

    @app.post("/api/v1/staged/batch-accept", dependencies=[Depends(require_auth)])
    def staged_batch_accept(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """**显式动作**：按给出的暂存 id 批量采用——单条「采用」的批量版。

        与 `safe-batch/accept` 的分工：那份只收「硬闸门全过」的项；这里
        **硬闸门未过也允许**，因为勾选本身就是人的判断（艺人相似度不足的
        翻唱、版本不一致的现场，就靠它成批过），口径与界面上单条「采用」
        按钮一致。只处理仍处于待审查状态的行，其余计入 `missing`。
        同一曲目的其余待审查候选一并驳回——单条采用在界面上就是这么做的，
        不驳回的话采用过的歌还会一直留在队列里。
        """
        payload = payload or {}
        ids_raw = payload.get("ids")
        if not isinstance(ids_raw, list) or not ids_raw:
            raise HTTPException(status_code=400, detail="ids 必须是非空数组")
        ids: list[int] = []
        for value in ids_raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="ids 里混有非数字") from None
        placeholders = ",".join("?" * len(ids))
        rows = state.conn.execute(
            f"SELECT id, location_id FROM staged_lyrics "
            f"WHERE id IN ({placeholders}) AND state='needs_review'",
            ids,
        ).fetchall()
        for row in rows:
            staged_id = int(row["id"])
            state.set_staged_state(staged_id, "staged")
            # 与单条 accept 同款：人工确认过的写入要做整份备份
            state.mark_reviewed(staged_id)
        rejected = 0
        if rows:
            accepted_ids = sorted({int(row["id"]) for row in rows})
            location_ids = sorted({int(row["location_id"]) for row in rows})
            id_marks = ",".join("?" * len(accepted_ids))
            loc_marks = ",".join("?" * len(location_ids))
            cur = state.conn.execute(
                f"UPDATE staged_lyrics SET state='rejected' "
                f"WHERE state='needs_review' "
                f"AND location_id IN ({loc_marks}) AND id NOT IN ({id_marks})",
                (*location_ids, *accepted_ids),
            )
            rejected = cur.rowcount or 0
        _invalidate_caches()
        return {
            "ok": True,
            "accepted": len(rows),
            "rejected": rejected,
            "missing": len(ids) - len(rows),
        }

    @app.post("/api/v1/staged/batch-ignore", dependencies=[Depends(require_auth)])
    def staged_batch_ignore(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """**显式动作**：真正忽略——为曲目写 path 级 `skip_fetch` 规则。

        「跳过」只驳回当前暂存行，下一轮扫描又会匹配出同样的候选刷回队列；
        忽略是曲库层面的人工判断（与标签提案的 ignored 同一级别），
        `run_fetch` 的规则排除会让这首歌从此不再取词、不再进待审查队列。
        反悔只有一个出口：删规则（「已忽略」标签的「恢复」）。
        同一曲目的全部待审查候选一并驳回，立刻离开两个标签。
        """
        payload = payload or {}
        ids_raw = payload.get("ids")
        if not isinstance(ids_raw, list) or not ids_raw:
            raise HTTPException(status_code=400, detail="ids 必须是非空数组")
        ids: list[int] = []
        for value in ids_raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="ids 里混有非数字") from None
        placeholders = ",".join("?" * len(ids))
        rows = state.conn.execute(
            f"SELECT DISTINCT s.location_id, l.rel_path FROM staged_lyrics s "
            f"JOIN locations l ON l.id=s.location_id "
            f"WHERE s.id IN ({placeholders}) AND s.state='needs_review'",
            ids,
        ).fetchall()
        for row in rows:
            state.add_rule(
                scope="path",
                target=str(row["rel_path"]),
                action="skip_fetch",
                reason="歌词审查：人工忽略",
            )
        rejected = 0
        if rows:
            location_ids = sorted({int(row["location_id"]) for row in rows})
            loc_marks = ",".join("?" * len(location_ids))
            cur = state.conn.execute(
                f"UPDATE staged_lyrics SET state='rejected' "
                f"WHERE state='needs_review' AND location_id IN ({loc_marks})",
                location_ids,
            )
            rejected = cur.rowcount or 0
        _invalidate_caches()
        return {"ok": True, "ignored": len(rows), "rejected": rejected}

    @app.get("/api/v1/rules", dependencies=[Depends(require_auth)])
    def list_rules() -> dict[str, Any]:
        """排除/纳入规则。目前唯一来源是歌词审查的「忽略」。"""
        items = [
            {
                "id": int(row["id"]),
                "scope": str(row["scope"]),
                "target": str(row["target"]),
                "action": str(row["action"]),
                "reason": row["reason"],
                "created_at": float(row["created_at"] or 0),
                "expires_at": float(row["expires_at"]) if row["expires_at"] else None,
            }
            for row in state.iter_rules()
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"total": len(items), "items": items}

    @app.delete("/api/v1/rules/{rule_id}", dependencies=[Depends(require_auth)])
    def delete_rule(rule_id: int) -> dict[str, Any]:
        """删规则（恢复）。下一轮抓取会重新为这首歌取词。"""
        if not state.remove_rule(rule_id):
            raise HTTPException(status_code=404, detail="不存在")
        return {"ok": True, "id": rule_id}

    @app.get("/api/v1/staged/{staged_id}", dependencies=[Depends(require_auth)])
    def staged_detail(staged_id: int) -> dict[str, Any]:
        row = state.conn.execute(
            "SELECT s.*, l.rel_path FROM staged_lyrics s JOIN locations l ON l.id=s.location_id "
            "WHERE s.id=?",
            (staged_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="不存在")

        # 把「各来源的命中情况」一并返回。
        # 只显示胜出来源会让人以为另一个源没查到——实际 Still Breathing 在
        # QQ 与 LRCLIB 上都命中了，但只记 LRCLIB，看起来就像 QQ 没被查询。
        providers: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        recommendation: dict[str, Any] | None = None
        if row["provider_id"]:
            res = state.conn.execute(
                "SELECT candidate_json FROM resolutions WHERE provider=? AND provider_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (row["provider"], row["provider_id"]),
            ).fetchone()
            if res is not None and res["candidate_json"]:
                try:
                    payload = json.loads(res["candidate_json"])
                    providers = payload.get("providers") or []
                    checks = payload.get("checks") or []
                    recommendation = payload.get("recommendation")
                except (TypeError, ValueError):
                    providers, checks, recommendation = [], [], None

        return {
            "id": row["id"],
            "rel_path": row["rel_path"],
            "content": row["content"],
            "state": row["state"],
            "provider": row["provider"],
            "provider_id": row["provider_id"],
            "score": row["score"],
            "duration_delta": row["duration_delta"],
            "providers": providers,
            "checks": checks,
            "recommendation": recommendation,
            "provider_order": list(cfg.providers.enabled),
        }

    @app.get(
        "/api/v1/staged/{staged_id}/alternatives",
        dependencies=[Depends(require_auth)],
    )
    def staged_alternatives(staged_id: int) -> dict[str, Any]:
        """取回该曲目在**各来源**上的候选与歌词，供人工对比后选定。

        会实时访问各来源（每源最多 2 次歌词请求）。逐源独立：某个来源失败
        不影响其它来源的展示——否则一个源挂掉会让人误以为「三个源都没有」。
        """
        row = state.conn.execute(
            "SELECT s.*, l.rel_path, l.root_id FROM staged_lyrics s "
            "JOIN locations l ON l.id=s.location_id WHERE s.id=?",
            (staged_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="不存在")

        root = _root_for_location(cfg, state, row["root_id"])
        from .providers import create_providers

        providers = create_providers(cfg.providers)
        try:
            result = build_comparison(
                cfg,
                root=root,
                rel_path=row["rel_path"],
                providers=providers,
                current_provider=row["provider"] or "",
                current_provider_id=row["provider_id"] or "",
            )
        finally:
            for provider in providers:
                provider.close()

        result["staged_id"] = staged_id
        result["current"] = {
            "provider": row["provider"],
            "provider_id": row["provider_id"],
        }
        return result

    @app.post(
        "/api/v1/staged/{staged_id}/choose",
        dependencies=[Depends(require_auth)],
    )
    def staged_choose(staged_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """采用指定来源的歌词作为该曲目待写入的内容。

        **只接受 `provider` + `provider_id`，不接受客户端提交歌词正文。**
        正文一律由服务端重新向来源取回——否则这个接口就成了「往音乐库里
        写任意内容」的入口，而这正是本工具最不该有的东西。
        """
        provider_name = str(payload.get("provider") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip()
        if not provider_name or not provider_id:
            raise HTTPException(
                status_code=400, detail="必须提供 provider 与 provider_id"
            )

        row = state.conn.execute(
            "SELECT s.*, l.rel_path, l.root_id FROM staged_lyrics s "
            "JOIN locations l ON l.id=s.location_id WHERE s.id=?",
            (staged_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="不存在")

        root = _root_for_location(cfg, state, row["root_id"])
        from .providers import create_provider

        try:
            provider = create_provider(provider_name, cfg.providers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        meta = read_audio_meta(root.resolved_path / row["rel_path"], root.resolved_path)
        context = build_context(meta.tags, meta.info.duration)
        query = TrackQuery(
            title=context.title,
            artists=context.artists,
            album=context.album,
            duration=context.duration,
            musicbrainz_trackid=context.musicbrainz_trackid,
            isrc=context.isrc,
        )

        try:
            candidates = provider.search(query)
            candidate = next(
                (c for c in candidates if c.provider_id == provider_id), None
            )
            if candidate is None:
                raise HTTPException(
                    status_code=404,
                    detail="该来源已找不到这个候选（可能已下架），请重新刷新对比列表",
                )
            raw = provider.lyrics(candidate)
        except HTTPException:
            raise
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=f"来源请求失败：{exc}") from exc
        finally:
            provider.close()

        if raw is None or not raw.text.strip():
            raise HTTPException(status_code=409, detail="该来源这首歌没有可用歌词")

        scores = score_candidates(
            context, [candidate], cfg.policy.matching,
            provider_order=cfg.providers.enabled,
        )
        score = scores[0] if scores else None
        digest = content_hash(normalize_lrc(raw.text))

        new_id = state.replace_staged(
            staged_id=staged_id,
            content_hash=digest,
            content=raw.text,
            provider=provider_name,
            provider_id=provider_id,
            score=score.score if score else None,
            duration_delta=score.duration_delta if score else None,
            translation=raw.translation or None,
            raw_qrc=raw.raw_text if cfg.lyrics.keep_word_level else None,
        )
        return {
            "ok": True,
            "staged_id": new_id,
            "provider": provider_name,
            "provider_id": provider_id,
            "content_hash": digest,
            "bytes": len(raw.text.encode("utf-8")),
        }

    @app.post("/api/v1/staged/{staged_id}/accept", dependencies=[Depends(require_auth)])
    def accept_staged(staged_id: int) -> dict[str, Any]:
        """把复核项改为已接受，之后 `apply` 才会写入它。

        同时标记 `origin='review'`：人工确认过的写入会做整份备份，
        而系统自动接受的那一类不做（见 WriteConfig.backup_review_only）。
        """
        result = _set_staged(state, staged_id, "staged")
        state.mark_reviewed(staged_id)
        return result

    @app.post("/api/v1/staged/{staged_id}/reject", dependencies=[Depends(require_auth)])
    def reject_staged(staged_id: int) -> dict[str, Any]:
        return _set_staged(state, staged_id, "rejected")

    # -- 单曲手工选歌词（按路径，不需要先有暂存记录）--------------------
    #
    # 为什么需要这一组接口：音乐库页上的「刮削歌词」原来是「点一下、后台跑完、
    # 结果好坏都看不到」。自动匹配有两种情况**必然**需要人来选：
    #   1. 上游存在上传者冒名记录（标题、时长都对，歌手字段是别人）；
    #   2. 本地 TITLE 标签本身被写坏（例如整个署名串被塞进标题），
    #      正确候选的标题相似度天然很低。
    # 这两种情况下自动匹配给不出结果，而「换一个来源试试」也没有出路——
    # 只能是「把各来源的候选都摆出来，让人挑」。
    #
    # 三个接口有意分开，是为了让每一步的代价都小：
    #   candidates  每来源 1 次搜索请求（不取歌词）
    #   lyrics-preview  只取被点中的那一条的歌词（1 次请求）
    #   apply-lyrics    服务端按 provider + id 重新取回并写入
    #     （**绝不接受客户端提交歌词正文**——否则这就成了往音乐库里写任意
    #      内容的入口）

    @app.get("/api/v1/tracks/candidates", dependencies=[Depends(require_auth)])
    def track_candidates(
        path: str,
        root: str | None = None,
        per_provider: int = Query(default=8, ge=1, le=20),
    ) -> dict[str, Any]:
        """列出该曲目在各来源上的候选（含逐闸门判定），不取歌词。"""
        root_cfg, abs_path, rel = _locate_audio(cfg, root, path)
        from .compare import list_candidates
        from .providers import create_providers

        providers = create_providers(cfg.providers)
        try:
            result = list_candidates(
                cfg, root=root_cfg, rel_path=rel,
                providers=providers, per_provider=per_provider,
            )
        finally:
            for provider in providers:
                provider.close()
        result["root"] = root_cfg.name
        result["size"] = abs_path.stat().st_size if abs_path.is_file() else None
        # **由服务端告诉界面「这个文件是否已有歌词」。**
        # 界面曾自己按 `embedded_state` 判断，漏掉 `.lrc`：文件只有外挂歌词时，
        # 对话框说「当前无歌词」，而写入接口按内嵌+外挂判 409，使用者被卡死——
        # 报错让他「勾选覆盖已有歌词」，可那个勾选框根本没渲染出来。
        row = state.conn.execute(
            "SELECT id FROM locations WHERE root_id=(SELECT id FROM roots WHERE name=?)"
            " AND rel_path=?", (root_cfg.name, rel),
        ).fetchone()
        result["has_existing_lyrics"] = (
            _has_existing_lyrics(state, int(row["id"])) if row is not None else False
        )
        return result

    @app.post("/api/v1/tracks/resolve-link", dependencies=[Depends(require_auth)])
    def track_resolve_link(payload: dict[str, Any]) -> dict[str, Any]:
        """把「使用者自己粘贴的歌曲链接」解析成一个可直接采用的候选。

        为什么需要它：自动匹配有两类**必然**失败的情况，而且调参救不回来——
        上游的上传者冒名记录，以及同名歌曲几十条（`存在` 在 QQ 上就有 20 多个
        不同录音）。这时使用者自己知道答案，让他贴一个链接是最省事的出路。

        这里**不跑匹配闸门**：链接本身就是「我确定是这一首」的人工断言。
        但正文仍然由服务端按 (来源, id) 重新取回，接口不接受歌词内容。
        """
        from .links import UnsupportedLink, parse_song_link

        try:
            # 注入短链跳转器：QQ 音乐 App 分享出来的是 c6.y.qq.com 短链，
            # 必须跟随 302 才能拿到 songmid（纯解析做不到，所以走回调）。
            link = parse_song_link(
                str(payload.get("url") or ""), resolver=_resolve_short_link
            )
        except UnsupportedLink as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        root_cfg, _abs, rel = _locate_audio(cfg, payload.get("root"), payload.get("path"))
        raw = _fetch_candidate_lyrics(
            cfg, root_cfg, rel, link.provider, link.song_id, allow_direct=True
        )

        from .providers import create_provider

        detail: dict[str, Any] | None = None
        provider = create_provider(link.provider, cfg.providers)
        try:
            fetch_detail = getattr(provider, "song_detail", None)
            if callable(fetch_detail):
                detail = fetch_detail(link.song_id)
        except Exception:  # noqa: BLE001 — 详情只是确认信息，取不到不影响取词
            detail = None
        finally:
            provider.close()

        parsed = parse_lrc(raw.text)
        return {
            "provider": link.provider,
            "provider_id": link.song_id,
            "url": link.original,
            "detail": detail,
            "synced": raw.synced,
            "word_level": raw.word_level,
            "has_translation": bool(raw.translation),
            "line_count": parsed.line_count,
            "timestamp_count": parsed.timestamp_count,
            "bytes": len(raw.text.encode("utf-8")),
            "text": raw.text,
            "lines": [
                {"ms": ln.ms, "stamp": ln.stamp, "text": ln.text}
                for ln in parse_lrc_lines(raw.text)
            ],
        }

    @app.get("/api/v1/tracks/lyrics-preview", dependencies=[Depends(require_auth)])
    def track_lyrics_preview(
        path: str,
        provider: str,
        provider_id: str,
        root: str | None = None,
        by_id: bool = False,
    ) -> dict[str, Any]:
        """取回某一条候选的歌词正文，供采用前预览。只读，不落盘。

        `by_id=true` 表示这是人工链接指定的候选（见 `_fetch_candidate_lyrics`）。
        """
        root_cfg, abs_path, rel = _locate_audio(cfg, root, path)
        raw = _fetch_candidate_lyrics(
            cfg, root_cfg, rel, provider, provider_id, allow_direct=by_id
        )
        parsed = parse_lrc(raw.text)
        # 音频时长与歌词时间轴的关系。同一首歌的短版常常配了完整版的
        # 时间轴——内容对得上，末行却超出音频几十秒。这种文件会被判
        # 「时间轴异常」，而界面必须在采用之前就把它摆出来，
        # 否则使用者只会看到一句「写入失败」。
        duration: float | None = None
        try:
            duration = read_audio_meta(abs_path, root_cfg.resolved_path).info.duration
        except Exception:  # noqa: BLE001 — 时长取不到不影响预览
            duration = None
        overrun: float | None = None
        last_ms = parsed.last_timestamp_ms
        if duration and duration > 0 and last_ms is not None:
            overrun = max(0.0, (last_ms - int(duration * 1000)) / 1000.0)
        return {
            "provider": provider,
            "provider_id": provider_id,
            "synced": raw.synced,
            "word_level": raw.word_level,
            "has_translation": bool(raw.translation),
            "line_count": parsed.line_count,
            "timestamp_count": parsed.timestamp_count,
            "duration_seconds": duration,
            "last_timestamp_ms": last_ms,
            "overrun_seconds": overrun,
            "text": raw.text,
            "lines": [
                {"ms": ln.ms, "stamp": ln.stamp, "text": ln.text}
                for ln in parse_lrc_lines(raw.text)
            ],
        }

    @app.post("/api/v1/tracks/apply-lyrics", dependencies=[Depends(require_auth)])
    def track_apply_lyrics(payload: dict[str, Any]) -> dict[str, Any]:
        """采用指定来源的某一条候选，并（可选）立即写入该曲目。

        `replace_existing=true` 时允许覆盖文件里已有的歌词——
        人工明确选了这一条，就不该被「已有同步歌词」的跳过规则挡回去。
        代价是这一条会走完整备份（origin='manual'）。
        """
        root_cfg, _abs, rel = _locate_audio(cfg, payload.get("root"), payload.get("path"))
        provider_name = str(payload.get("provider") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip()
        # 手动编辑直写模式（歌词编辑器）：text 与 provider/provider_id 二选一。
        # 编辑器的价值在于「人只改一两行」——没有 provider 可言，直接落盘。
        # 手动编辑直写模式（歌词编辑器）：只给 text、不给来源时，直接采用
        # 编辑器内容落盘。给了 provider/provider_id 时 text 一律忽略、以
        # 来源取回的正文为准——那是「防伪造」契约（见 test_manual_lyrics_api）：
        # 服务器绝不能把客户端声称的正文当成某来源的歌词写进文件。
        editor_text = payload.get("text")
        editor_text = (
            editor_text
            if isinstance(editor_text, str) and editor_text.strip()
            and not provider_name and not provider_id
            else ""
        )
        editor_translation = payload.get("translation")
        editor_translation = (
            editor_translation
            if isinstance(editor_translation, str) and editor_translation.strip()
            else None
        )
        if not editor_text and (not provider_name or not provider_id):
            raise HTTPException(
                status_code=400,
                detail="必须提供编辑文本（text），或 provider 与 provider_id",
            )
        apply_now = bool(payload.get("apply", True))
        replace_existing = bool(payload.get("replace_existing"))
        # 人工在界面上逐条选了候选并点了写入——那就是「要写进去」的意思。
        # 默认强制：即便文件里已经是同一份歌词也照写一遍。内容指纹相同
        # 不代表文件是对的（同一首歌的短版配了完整版的时间轴就是这种情况），
        # 把「已经满足」当成拒绝写入，会让人以为操作失败。
        force_rewrite = bool(payload.get("force_rewrite", True))
        if apply_now and not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )

        root_id = state.upsert_root(
            name=root_cfg.name, role=root_cfg.role.value,
            path=str(root_cfg.resolved_path), enabled=root_cfg.enabled,
            read_only=root_cfg.read_only,
        )
        location = state.location_by_rel_path(root_id, rel)
        if location is None:
            raise HTTPException(
                status_code=404,
                detail="该文件还没有登记过，请先让监控或一次抓取扫描到它",
            )
        location_id = int(location["id"])

        if editor_text:
            text = normalize_lrc(editor_text)
            digest = content_hash(text)
        else:
            raw = _fetch_candidate_lyrics(
                cfg, root_cfg, rel, provider_name, provider_id,
                allow_direct=bool(payload.get("by_id")),
            )
            text = normalize_lrc(raw.text)
            digest = content_hash(text)

        # 「文件里已经有歌词」必须由服务端拦，不能只靠界面上的勾选框：
        # 覆盖别人的歌词是不可逆的（虽然后台有备份），而界面可以被绕过。
        # 判据取自最近一次观测——它由审计/监控写入，正是「此刻文件里有什么」。
        if apply_now and not replace_existing and _has_existing_lyrics(state, location_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "这份文件里已经有歌词（内嵌或 .lrc 任一）。"
                    "要用选中的这一条替换，请先勾选「覆盖已有歌词」——"
                    "替换前会完整备份原文件。"
                ),
            )

        staged_id = state.stage_lyrics(
            location_id=location_id,
            content_hash=digest,
            content=text,
            provider=provider_name or "manual",
            provider_id=provider_id or "editor",
            state="staged",
            origin="manual",
            translation=editor_translation,
        )
        # 文件里已经是这一份了吗？写完要如实告诉使用者，不能拿
        # 「无需重复写入」当成功回执——那看着就像写入失败。
        already_same = _lyrics_already_same(state, location_id, digest)
        # 同一位置的其它待写入行（历史抓取留下的）就此作废：
        # 人工只认他刚选中的这一条。留着它们，批量写入会把旧的那条也写一遍，
        # 最终内容取决于迭代顺序——那是不可接受的随机性。
        state.supersede_staged_except(location_id, staged_id)

        out: dict[str, Any] = {
            "ok": True,
            "staged_id": staged_id,
            "content_hash": digest,
            "bytes": len(text.encode("utf-8")),
            "applied": None,
        }
        if not apply_now:
            return out

        from .pipeline import run_apply

        # **人工优先**：等监控让出，而不是把「采用并写入」降级成「已选用」。
        with _manual_lease(state, wait_seconds=60, label="manual-lyrics") as lease:
            if not lease["acquired"]:
                out["applied"] = {
                    "ok": False,
                    "error": f"写入任务仍在运行（{lease['holder']}），请稍后重试",
                }
                return out
            summary = run_apply(
                state, cfg, root_cfg,
                only_rel_paths={rel},
                # 只写**刚选中的这一条**，不是这个位置的全部待写入行。
                only_staged_ids={staged_id},
                # 人工选定就是决定：即便这条歌词没有时间轴，也要写进去。
                force=True,
                # 内容相同也照写：人工点了「采用并写入」，要的就是落盘。
                force_rewrite=force_rewrite,
                progress=lambda _line: None,
            )

        # 达成目标就算成功：写入成功，或文件里本来就已经是这一份歌词。
        written = summary.applied
        already = summary.skipped_satisfied
        failed = summary.apply_failed
        errors = summary.errors[:5]
        if failed:
            message = errors[0] if errors else "写入失败"
        elif written:
            message = (
                "已强制写入（文件里原本就是这一份，内容与时间轴按这一条重写）"
                if (force_rewrite and already_same)
                else ""
            )
        elif already:
            message = "这份文件里已经是选中的这份歌词，无需重复写入"
        else:
            message = "没有写入任何歌词，请查看该文件的状态后重试"
        out["applied"] = {
            "ok": bool((written or already) and not failed),
            "written": written,
            "satisfied": already,
            "skipped_satisfied": summary.skipped_satisfied,
            "failed": failed,
            "deferred_locked": summary.deferred_locked,
            "errors": errors,
            "error": message,
            "message": message,
        }
        _invalidate_caches()
        return out

    @app.get("/api/v1/issues", dependencies=[Depends(require_auth)])
    def issues(page: int = 1, size: int = Query(default=100, le=500)) -> dict[str, Any]:
        return _issues(state, page=page, size=size)

    # -- 曲库整理 ---------------------------------------------------------

    @app.post("/api/v1/organize/preview", dependencies=[Depends(require_auth)])
    def organize_preview(payload: dict[str, Any]) -> dict[str, Any]:
        """生成「旧路径 → 新路径」预览，不移动任何文件。"""
        from .organize import build_plan

        root = _select_root(cfg, payload.get("root") or None)
        subpaths, exclude = _organize_scope(payload)
        subpath = subpaths[0] if len(subpaths) == 1 else ""
        try:
            plan = build_plan(
                state, cfg, root, subpaths=subpaths, exclude=exclude,
                subpath=subpath,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        root_id = state.get_root_id(root.name)
        if root_id is None:
            root_id = state.upsert_root(
                name=root.name, role=root.role.value, path=str(root.resolved_path),
                enabled=root.enabled, read_only=root.read_only,
            )
        state.save_organize_plan(
            plan_id=str(plan["plan_id"]),
            root_id=int(root_id),
            subpath=subpath,
            payload=plan,
        )
        return plan

    @app.post("/api/v1/organize/execute", dependencies=[Depends(require_auth)])
    @manual_lease()
    def organize_execute(payload: dict[str, Any]) -> dict[str, Any]:
        """执行用户看过的那一份预览；计划过期或已执行则拒绝。"""
        from .organize import execute_plan

        plan_id = str(payload.get("plan_id") or "")
        if not plan_id:
            raise HTTPException(status_code=400, detail="缺少 plan_id")
        row = state.organize_plan(plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail="预览已过期，请重新生成")
        if row["consumed_at"]:
            raise HTTPException(status_code=409, detail="这份预览已经执行过")
        if time.time() - float(row["created_at"] or 0) > 6 * 3600:
            raise HTTPException(status_code=409, detail="预览已超过 6 小时，请重新生成")
        root = next(
            (r for r in cfg.roots if state.get_root_id(r.name) == row["root_id"]), None
        )
        if root is None:
            raise HTTPException(status_code=400, detail="该预览所属受管根当前未配置")
        if not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        if root.read_only:
            raise HTTPException(status_code=409, detail="该受管根是只读的")
        dirs = payload.get("dirs")
        result = execute_plan(
            state, cfg, root, row, dirs=[str(d) for d in dirs] if isinstance(dirs, list) else []
        )
        _invalidate_caches()
        return result

    @app.post("/api/v1/organize/cleanup-preview", dependencies=[Depends(require_auth)])
    def organize_cleanup_preview(payload: dict[str, Any]) -> dict[str, Any]:
        """只列出没有音频的目录，不读取标签、不移动文件。"""
        from .organize import preview_cleanup

        root = _select_root(cfg, payload.get("root") or None)
        subpaths, exclude = _organize_scope(payload)
        subpath = subpaths[0] if len(subpaths) == 1 else ""
        items = preview_cleanup(
            root, subpaths=subpaths, exclude=exclude, subpath=subpath
        )
        return {
            "root": root.name,
            "subpaths": subpaths,
            "exclude": exclude,
            "subpath": subpath,
            "items": items,
            "empty": sum(1 for item in items if item["kind"] == "empty"),
            "no_audio": sum(1 for item in items if item["kind"] == "no_audio"),
        }

    @app.post("/api/v1/organize/cleanup", dependencies=[Depends(require_auth)])
    @manual_lease()
    def organize_cleanup(payload: dict[str, Any]) -> dict[str, Any]:
        """清理空目录 / 无音频目录；路径必须来自刚刚的预览。"""
        from .organize import cleanup_dirs

        root = _select_root(cfg, payload.get("root") or None)
        if not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        if root.read_only:
            raise HTTPException(status_code=409, detail="该受管根是只读的")
        subpaths, exclude = _organize_scope(payload)
        subpath = subpaths[0] if len(subpaths) == 1 else ""
        paths = payload.get("paths") or []
        if not isinstance(paths, list):
            raise HTTPException(status_code=400, detail="paths 必须是数组")
        result = cleanup_dirs(
            state, cfg, root, subpaths=subpaths, exclude=exclude,
            subpath=subpath, paths=[str(p) for p in paths],
        )
        _invalidate_caches()
        return result

    @app.get("/api/v1/organize/ops", dependencies=[Depends(require_auth)])
    def organize_ops(
        limit: int = Query(default=300, ge=1, le=2000),
        include_undone: bool = False,
    ) -> dict[str, Any]:
        rows = state.organize_ops(limit=limit, active_only=not include_undone)
        return {
            "items": [_organize_op_dict(row) for row in rows],
            "total": len(rows),
        }

    @app.post("/api/v1/organize/undo", dependencies=[Depends(require_auth)])
    def organize_undo(payload: dict[str, Any]) -> dict[str, Any]:
        """单条或批量撤销整理操作；按时间倒序，逐条报告失败原因。"""
        from .organize import undo_organize_ops

        raw = payload.get("ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="必须提供 ids")
        ids = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="ids 必须是整数") from exc
        result = undo_organize_ops(state, cfg, ids)
        _invalidate_caches()
        return result

    def _dedupe_group_payload(row: Any) -> dict[str, Any]:
        try:
            items = json.loads(str(row["items_json"]))
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        return {
            "id": int(row["id"]),
            "root": str(row["root_name"]),
            "kind": str(row["kind"]),
            "keep_rel_path": str(row["keep_rel_path"]),
            "keep_reason": str(row["keep_reason"]),
            "items": items,
            "found_at": float(row["found_at"] or 0),
            "dismissed_at": float(row["dismissed_at"] or 0) or None,
        }

    @app.get("/api/v1/dedupe/groups", dependencies=[Depends(require_auth)])
    def dedupe_groups() -> dict[str, Any]:
        groups = []
        dismissed = []
        duplicates = 0
        for row in state.iter_dedupe_groups(include_dismissed=True):
            payload = _dedupe_group_payload(row)
            if payload["dismissed_at"]:
                dismissed.append(payload)
                continue
            duplicates += sum(
                1 for item in payload["items"]
                if isinstance(item, dict) and item.get("suggested_delete")
            )
            groups.append(payload)
        raw = state.get_meta("dedupe.last_scan_summary")
        last = {}
        if raw:
            try:
                last = json.loads(raw)
            except (TypeError, ValueError):
                last = {}
        return {
            "groups": groups,
            "dismissed": dismissed,
            "duplicates": duplicates,
            "last_scan_at": float(state.get_meta("dedupe.last_scan_at") or 0) or None,
            "last_scan": last,
        }

    @app.post("/api/v1/dedupe/groups/{group_id}/delete", dependencies=[Depends(require_auth)])
    @manual_lease()
    def dedupe_delete(group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        from .dedupe import delete_duplicates

        row = state.dedupe_group(group_id)
        if row is None:
            raise HTTPException(status_code=404, detail="重复组不存在，请重新扫描")
        if row["dismissed_at"]:
            raise HTTPException(status_code=404, detail="这一组已被忽略，先恢复再处理")
        root = next((r for r in cfg.roots if r.name == row["root_name"]), None)
        if root is None:
            raise HTTPException(status_code=400, detail="该记录所属受管根当前未配置")
        if not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        if root.read_only:
            raise HTTPException(status_code=409, detail="该受管根是只读的")
        raw = payload.get("paths")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="必须提供要删除的 paths")
        result = delete_duplicates(
            state, cfg, root, row, [str(p) for p in raw]
        )
        _invalidate_caches()
        if not result.get("ok") and not result.get("deleted"):
            raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
        return result

    @app.post("/api/v1/dedupe/delete", dependencies=[Depends(require_auth)])
    @manual_lease()
    def dedupe_delete_bulk(payload: dict[str, Any]) -> dict[str, Any]:
        """按分类或按勾选项批量删除重复副本。

        `kind` 只处理该分类里「建议删除」的副本，绝不会跨分类；
        `items` 用于界面上手动改选后的批量删除，后端逐项校验归属与状态。
        """
        from .dedupe import delete_duplicates_selected

        raw_kind = payload.get("kind")
        selections: dict[int, list[str]] = {}
        if raw_kind is not None:
            kind = str(raw_kind)
            if kind not in ("exact", "recording", "similar"):
                raise HTTPException(status_code=400, detail="未知的重复分类")
            for row in state.iter_dedupe_groups():
                if str(row["kind"]) != kind:
                    continue
                try:
                    items = json.loads(str(row["items_json"]))
                except (TypeError, ValueError):
                    continue
                paths = [
                    str(item.get("rel_path"))
                    for item in items
                    if isinstance(item, dict)
                    and item.get("suggested_delete")
                    and not item.get("reference")
                ]
                if paths:
                    selections[int(row["id"])] = paths
        else:
            raw_items = payload.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise HTTPException(status_code=400, detail="必须提供 kind 或 items")
            for item in raw_items:
                if not isinstance(item, dict):
                    raise HTTPException(status_code=400, detail="items 必须是对象数组")
                try:
                    group_id = int(item.get("group_id"))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="group_id 必须是整数") from exc
                path = str(item.get("path") or "")
                if not path:
                    raise HTTPException(status_code=400, detail="path 不能为空")
                selections.setdefault(group_id, []).append(path)

        if not selections:
            return {"ok": True, "deleted": 0, "errors": [], "batch_ids": []}
        result = delete_duplicates_selected(state, cfg, selections)
        _invalidate_caches()
        if not result.get("ok") and not result.get("deleted"):
            raise HTTPException(status_code=409, detail="；".join(result.get("errors") or ["删除失败"]))
        return result

    @app.post("/api/v1/dedupe/groups/{group_id}/dismiss", dependencies=[Depends(require_auth)])
    def dedupe_dismiss(group_id: int) -> dict[str, Any]:
        """忽略这一组。忽略会一直有效，直到组里的文件组合发生变化。"""
        if not state.dismiss_dedupe_group(group_id):
            raise HTTPException(status_code=404, detail="重复组不存在")
        return {"ok": True}

    @app.post("/api/v1/dedupe/groups/{group_id}/restore", dependencies=[Depends(require_auth)])
    def dedupe_restore(group_id: int) -> dict[str, Any]:
        """取消忽略，这一组重新回到待处理列表。"""
        if not state.restore_dedupe_group(group_id):
            raise HTTPException(status_code=404, detail="重复组不存在或没有被忽略")
        return {"ok": True}

    @app.post("/api/v1/tasks/dedupe-scan", dependencies=[Depends(require_auth)])
    def start_dedupe_scan(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """手动触发一次重复检测。只读扫描，不删除任何文件。"""
        from .dedupe import scan_duplicates

        payload = payload or {}
        root_name = str(payload.get("root") or "")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            return scan_duplicates(state, cfg, roots, progress=progress)

        return runner.start("dedupe-scan", job).to_dict()

    @app.get("/api/v1/renames", dependencies=[Depends(require_auth)])
    def renames() -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": r["id"], "old": r["old_rel_path"], "new": r["new_rel_path"],
                    "reason": r["reason"], "at": r["renamed_at"],
                }
                for r in state.iter_renames()
            ]
        }

    @app.post("/api/v1/renames/batch-undo", dependencies=[Depends(require_auth)])
    def batch_undo_renames(payload: dict[str, Any]) -> dict[str, Any]:
        """批量撤销改名。按记录倒序处理，单项冲突不影响其它项。"""
        from .rename import undo_rename

        raw = payload.get("ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(status_code=400, detail="必须提供 ids")
        undone = 0
        failed = 0
        messages: list[str] = []
        rows = []
        for value in raw:
            try:
                rename_id = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="ids 必须是整数") from exc
            row = state.conn.execute(
                "SELECT * FROM renames WHERE id=?", (rename_id,)
            ).fetchone()
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda r: float(r["renamed_at"] or 0), reverse=True)
        for row in rows:
            root = next(
                (r for r in cfg.roots if state.get_root_id(r.name) == row["root_id"]),
                None,
            )
            if root is None:
                failed += 1
                messages.append(f"#{row['id']} 所属受管根当前未配置")
                continue
            ok, message = undo_rename(state, root, int(row["id"]))
            if ok:
                undone += 1
            else:
                failed += 1
                messages.append(f"#{row['id']} {message}")
        _invalidate_caches()
        return {"ok": failed == 0, "undone": undone, "failed": failed, "messages": messages}

    @app.post("/api/v1/renames/{rename_id}/undo", dependencies=[Depends(require_auth)])
    def undo_rename_api(rename_id: int) -> dict[str, Any]:
        from .rename import undo_rename

        row = state.conn.execute("SELECT * FROM renames WHERE id=?", (rename_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="不存在")
        root = next(
            (r for r in cfg.roots if state.get_root_id(r.name) == row["root_id"]), None
        )
        if root is None:
            raise HTTPException(status_code=400, detail="该记录所属受管根当前未配置")
        ok, message = undo_rename(state, root, rename_id)
        return {"ok": ok, "message": message}

    @app.get("/api/v1/writes", dependencies=[Depends(require_auth)])
    def writes() -> dict[str, Any]:
        rows = state.conn.execute(
            "SELECT w.*, l.rel_path FROM sidecar_writes w "
            "LEFT JOIN locations l ON l.id=w.location_id "
            "WHERE w.undone_at IS NULL ORDER BY w.written_at DESC LIMIT 200"
        ).fetchall()
        total = state.conn.execute(
            "SELECT COUNT(*) FROM sidecar_writes WHERE undone_at IS NULL"
        ).fetchone()[0]
        return {
            "items": [
                {
                    "id": r["id"], "path": r["sidecar_path"], "rel_path": r["rel_path"],
                    "created_new": bool(r["created_new"]), "at": r["written_at"],
                }
                for r in rows
            ],
            "total": total,
            "keep": _AUDIT_KEEP,
        }

    @app.delete("/api/v1/writes/{write_id}", dependencies=[Depends(require_auth)])
    def delete_write(write_id: int) -> dict[str, Any]:
        """删除一条写入记录（只删记录，文件与备份都不动）。"""
        with state.transaction() as tx:
            tx.execute("DELETE FROM sidecar_writes WHERE id=?", (write_id,))
        return {"ok": True}

    @app.post("/api/v1/writes/clear", dependencies=[Depends(require_auth)])
    def clear_writes() -> dict[str, Any]:
        with state.transaction() as tx:
            n = tx.execute("DELETE FROM sidecar_writes").rowcount
        return {"ok": True, "deleted": max(0, n)}

    @app.post("/api/v1/writes/{write_id}/undo", dependencies=[Depends(require_auth)])
    def undo_write(write_id: int) -> dict[str, Any]:
        from .writer import undo_sidecar_write

        ok, message = undo_sidecar_write(state, write_id)
        return {"ok": ok, "message": message}

    @app.get("/api/v1/transactions", dependencies=[Depends(require_auth)])
    def transactions() -> dict[str, Any]:
        rows = state.conn.execute(
            "SELECT * FROM transactions ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
        total = state.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        return {
            "items": [
                {
                    "txn_id": r["txn_id"], "state": r["state"], "error": r["error"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ],
            "unfinished": len(state.unfinished_transactions()),
            "total": total,
            "keep": _AUDIT_KEEP,
        }

    @app.delete("/api/v1/transactions/{txn_id}", dependencies=[Depends(require_auth)])
    def delete_transaction(txn_id: str) -> dict[str, Any]:
        """删除一条已完结的事务记录。未完结的拒绝删除（可能还要恢复）。"""
        row = state.conn.execute(
            "SELECT state FROM transactions WHERE txn_id=?", (txn_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="不存在")
        if row["state"] not in (
            "completed", "rolled_back", "recovery_conflict", "sidecar_committed",
        ):
            raise HTTPException(status_code=409, detail="未完结的事务不能删除")
        with state.transaction() as tx:
            tx.execute("DELETE FROM transactions WHERE txn_id=?", (txn_id,))
        return {"ok": True}

    @app.post("/api/v1/transactions/clear", dependencies=[Depends(require_auth)])
    def clear_transactions() -> dict[str, Any]:
        with state.transaction() as tx:
            n = tx.execute(
                "DELETE FROM transactions WHERE state IN (?, ?, ?, ?)",
                ("completed", "rolled_back", "recovery_conflict", "sidecar_committed"),
            ).rowcount
        return {"ok": True, "deleted": max(0, n)}

    @app.get("/api/v1/config", dependencies=[Depends(require_auth)])
    def config_view() -> dict[str, Any]:
        return cfg.to_dict(mask_secrets=True)

    @app.get("/api/v1/settings/tags", dependencies=[Depends(require_auth)])
    def tag_settings_view() -> dict[str, Any]:
        return {
            "policy": asdict(cfg.policy.tags),
            "revision": state.get_meta("settings.revision") or "",
        }

    @app.put("/api/v1/settings/tags", dependencies=[Depends(require_auth)])
    def update_tag_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """保存标签刮削策略。写入数据库，监控进程会在一分钟内读取。"""
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        try:
            apply_tag_policy_values(cfg, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.set_meta(
            "settings.tags_policy", json.dumps(payload, ensure_ascii=False)
        )
        revision = str(time.time_ns())
        state.set_meta("settings.revision", revision)
        _invalidate_caches()
        return {
            "ok": True,
            "policy": asdict(cfg.policy.tags),
            "revision": revision,
        }

    def _network_settings_view() -> dict[str, Any]:
        proxy = cfg.network.proxy
        reachable: bool | None = None
        if proxy:
            reachable = proxy_reachable(proxy)
        return {
            "proxy": proxy,
            "source": cfg.network.proxy_source,
            "effective_proxy": proxy or environment_proxy(),
            "environment_proxy": environment_proxy(),
            "schemes": list(PROXY_SCHEMES),
            "features": list(PROXY_FEATURES),
            "use_proxy": dict(cfg.network.use_proxy),
            "proxy_reachable": reachable,
            "proxy_status": (
                "none" if not proxy else ("ok" if reachable else "unreachable")
            ),
        }

    @app.get("/api/v1/settings/network", dependencies=[Depends(require_auth)])
    def network_settings_view() -> dict[str, Any]:
        return _network_settings_view()

    @app.post("/api/v1/settings/network/test", dependencies=[Depends(require_auth)])
    def test_network_settings() -> dict[str, Any]:
        """探测代理端口能不能连上。

        代理常常跑在另一台机器上，那台机器一停代理，走代理的功能会
        全部失败——但报错只有一句「Connection refused」。这里给一个直接
        的「代理通不通」的答案，省得逐个功能去试。
        """
        proxy = cfg.network.proxy or environment_proxy()
        if not proxy:
            return {
                "ok": True,
                "proxy": "",
                "reachable": None,
                "message": "没有配置代理，所有请求直连",
            }
        reachable = proxy_reachable(proxy, force=True)
        return {
            "ok": True,
            "proxy": mask_proxy_url(proxy),
            "reachable": reachable,
            "message": (
                f"代理 {mask_proxy_url(proxy)} 可以连上"
                if reachable
                else f"代理 {mask_proxy_url(proxy)} 连不上，走代理的功能会失败"
            ),
        }

    @app.put("/api/v1/settings/network", dependencies=[Depends(require_auth)])
    def update_network_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """保存代理地址，并让后续发起的请求立即使用。"""
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        unknown = set(payload) - set(NETWORK_SETTINGS_FIELDS)
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知设置项：{sorted(unknown)[0]}")

        before = replace(cfg.network)
        try:
            apply_network_settings_values(cfg, payload)
            write_saved_network(
                cfg.storage.resolved_data_dir,
                cfg.network.proxy,
                cfg.network.use_proxy,
            )
            set_active_network(
                cfg.network.proxy or environment_proxy(),
                cfg.network.use_proxy,
                proxy_bypass_hosts(cfg),
            )
            cfg.network.proxy_source = (
                "settings"
                if cfg.network.proxy
                else ("env" if environment_proxy() else "none")
            )
        except ValueError as exc:
            cfg.network = before
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            cfg.network = before
            raise HTTPException(
                status_code=500, detail=f"保存代理设置失败：{exc}"
            ) from exc
        return {"ok": True, **_network_settings_view()}

    def _ai_settings_view() -> dict[str, Any]:
        env_key = os.environ.get("LYRA_AI_API_KEY") or ""
        saved_token = _read_ai_token(cfg)
        if env_key:
            source = "env"
        elif saved_token:
            source = "settings"
        elif cfg.ai.api_key.strip():
            source = "config"
        else:
            source = "none"
        return {
            "enabled": cfg.ai.enabled,
            "protocol": cfg.ai.protocol,
            "base_url": cfg.ai.base_url,
            "model": cfg.ai.model,
            "timeout_seconds": cfg.ai.timeout_seconds,
            "max_requests_per_run": cfg.ai.max_requests_per_run,
            "auto_clean_duplicates": cfg.ai.auto_clean_duplicates,
            "pre_review": cfg.ai.pre_review,
            "pre_review_confidence": cfg.ai.pre_review_confidence,
            "auto_accept": cfg.ai.auto_accept,
            "verify_tags": cfg.ai.verify_tags,
            "use_proxy": cfg.ai.use_proxy,
            "has_api_key": bool(cfg.ai.api_key.strip()),
            "api_key_source": source,
            "protocols": list(AI_PROTOCOLS),
            "revision": state.get_meta("settings.ai.revision") or "",
        }

    @app.get("/api/v1/settings/ai", dependencies=[Depends(require_auth)])
    def ai_settings_view() -> dict[str, Any]:
        return _ai_settings_view()

    @app.put("/api/v1/settings/ai", dependencies=[Depends(require_auth)])
    def update_ai_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """保存大模型配置；token 单独写入 0600 文件，不进入数据库。"""
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        unknown = set(payload) - set(AI_SETTINGS_FIELDS) - {"api_key", "clear_api_key"}
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知设置项：{sorted(unknown)[0]}")
        if os.environ.get("LYRA_AI_API_KEY") and (
            payload.get("clear_api_key") or payload.get("api_key") not in (None, "", "***")
        ):
            raise HTTPException(
                status_code=400,
                detail="环境变量 LYRA_AI_API_KEY 优先，无法从设置页修改 token",
            )

        before = replace(cfg.ai)
        values = {k: payload[k] for k in AI_SETTINGS_FIELDS if k in payload}
        try:
            apply_ai_settings_values(cfg, values)
            if payload.get("clear_api_key"):
                _clear_ai_token(cfg)
                cfg.ai.api_key = ""
            else:
                token = payload.get("api_key")
                if isinstance(token, str) and token not in ("", "***"):
                    token = token.strip()
                    _write_ai_token(cfg, token)
                    cfg.ai.api_key = token
            if cfg.ai.enabled and not cfg.ai.api_key.strip():
                raise ValueError("启用 AI 时必须填写 API Token")
            stored = {k: getattr(cfg.ai, k) for k in AI_SETTINGS_FIELDS}
            state.set_meta("settings.ai", json.dumps(stored, ensure_ascii=False))
            state.set_meta("settings.ai.revision", str(time.time_ns()))
        except ValueError as exc:
            cfg.ai = before
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            cfg.ai = before
            raise HTTPException(status_code=500, detail=f"保存 AI token 失败：{exc}") from exc
        _invalidate_caches()
        return {"ok": True, **_ai_settings_view()}

    @app.post("/api/v1/settings/ai/test", dependencies=[Depends(require_auth)])
    def test_ai_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """用当前表单或已保存配置发一条最小请求，不写任何文件。"""
        from .ai import AIError, test_connection

        payload = payload or {}
        temp_cfg = replace(cfg, ai=replace(cfg.ai))
        values = {k: payload[k] for k in AI_SETTINGS_FIELDS if k in payload}
        try:
            apply_ai_settings_values(temp_cfg, values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = payload.get("api_key")
        if isinstance(token, str) and token not in ("", "***"):
            temp_cfg.ai.api_key = token.strip()
        if not temp_cfg.ai.api_key.strip():
            raise HTTPException(status_code=400, detail="还没有填写 API Token")
        try:
            return test_connection(
                protocol=temp_cfg.ai.protocol,
                base_url=temp_cfg.ai.base_url,
                model=temp_cfg.ai.model,
                api_key=temp_cfg.ai.api_key,
                timeout=temp_cfg.ai.timeout_seconds,
                use_proxy=temp_cfg.ai.use_proxy,
            )
        except AIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- 动作 -------------------------------------------------------------

    @app.post("/api/v1/tasks/fetch", dependencies=[Depends(require_auth)])
    def start_fetch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        limit = payload.get("limit")
        root_name = payload.get("root")
        force = bool(payload.get("force"))
        raw_paths = payload.get("paths") or []
        paths = {str(p) for p in raw_paths if str(p).strip()} or None
        # apply=True：抓完立刻写入。用户要的是「完善歌词」，不是先攒一堆积压。
        apply_now = bool(payload.get("apply"))
        # 按目录刮削：`dir` 是相对受管根的目录（"" = 整个根）。
        # 由服务端展开成文件列表——让前端自己遍历目录既慢又容易越界。
        dir_prefix = str(payload.get("dir") or "").strip().strip("/")
        if ".." in dir_prefix.split("/"):
            raise HTTPException(status_code=400, detail="目录参数不能包含 ..")
        # 按「歌词现状」筛选：与目录/路径范围取交集。
        raw_filters = payload.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise HTTPException(status_code=400, detail="filters 必须是对象")
        lyric_state = str(raw_filters.get("lyric_state") or "")
        if lyric_state not in ("", "none", "unsynced"):
            raise HTTPException(status_code=400, detail="未知的歌词状态筛选")
        missing_sidecar = bool(raw_filters.get("missing_sidecar"))
        missing_embedded = bool(raw_filters.get("missing_embedded"))
        artist = str(raw_filters.get("artist") or "").strip()
        # 「歌词现状」类筛选走数据库里的观测；艺人筛选走文件标签，
        # 两者**不能混在一起算**：观测表要等第一次审计才有数据，
        # 新拷进来的目录会因此被筛成空集。
        has_lyric_filters = bool(lyric_state or missing_sidecar or missing_embedded)
        has_filters = has_lyric_filters or bool(artist)

        from .pipeline import fill_sidecars, run_apply, run_fetch
        from .providers import create_providers

        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        if apply_now and not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409, detail="配置里写入开关已关闭"
            )

        def _dir_paths(root_cfg: RootConfig) -> set[str]:
            """把目录展开成音频相对路径集合（空目录返回空集合）。"""
            from . import scan

            base = root_cfg.resolved_path
            target = (base / dir_prefix).resolve() if dir_prefix else base
            if target != base and base not in target.parents:
                raise HTTPException(status_code=400, detail="目录不在受管根内")
            if not target.is_dir():
                raise HTTPException(status_code=404, detail=f"目录不存在：{dir_prefix}")
            return {
                str(path.relative_to(base))
                for path in scan.iter_audio_files(target, root_cfg)
            }

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            # **只在真的要写文件时才申请写租约。**
            # 纯抓取（apply_now=False）只把歌词暂存进数据库，不碰音乐文件；
            # 此前它也申请写租约并持有 15 分钟（TTL 900s），把人工写入的
            # 「开始写入」堵在门外——用户看到的就是「任务已开始，但一直没写入」。
            if not apply_now:
                return _fetch_job(progress)
            with _manual_lease(state, wait_seconds=120, label="manual-fetch") as lease:
                if not lease["acquired"]:
                    raise RuntimeError(
                        f"写入任务仍在运行（{lease['holder']}），请稍后重试")
                return _fetch_job(progress)

        def _fetch_job(progress: Callable[[str], None]) -> dict[str, Any]:
            providers = create_providers(cfg.providers)
            out: list[dict[str, Any]] = []
            try:
                for root in roots:
                    scope = set(paths) if paths else (
                        _dir_paths(root) if dir_prefix or payload.get("dir") is not None
                        else None
                    )
                    if has_lyric_filters:
                        root_row = state.conn.execute(
                            "SELECT id FROM roots WHERE name=?", (root.name,)
                        ).fetchone()
                        matched = _filter_by_lyrics(
                            state, int(root_row["id"]) if root_row else -1,
                            lyric_state=lyric_state,
                            missing_sidecar=missing_sidecar,
                            missing_embedded=missing_embedded,
                        )
                        scope = matched if scope is None else (scope & matched)
                    if scope is not None and not scope:
                        out.append({"root": root.name, "dir": dir_prefix,
                                    "scanned": 0, "fetched": 0, "accepted": 0,
                                    "errors": [
                                        "没有符合筛选条件的曲目"
                                        if has_filters else "该目录下没有音频文件"
                                    ]})
                        continue
                    if scope:
                        # **先补 .lrc，再联网。** 库里大量文件的内嵌歌词本来就是
                        # 带时间轴的 LRC，落成外挂文件是纯本地操作（毫秒级）；
                        # 先做这一步，联网就只剩真正没有歌词的那几首。
                        filled = fill_sidecars(
                            state, cfg, root, only_rel_paths=scope,
                        )
                        if filled.filled:
                            progress(f"本地补 .lrc {filled.filled} 首")
                    s = run_fetch(
                        state, cfg, root, providers,
                        limit=int(limit) if limit else None, force=force,
                        only_rel_paths=scope, artist=artist, progress=progress,
                    )
                    entry: dict[str, Any] = {
                        "root": root.name, "dir": dir_prefix, "scanned": s.scanned,
                        "considered": s.considered,
                        "fetched": s.fetched, "accepted": s.accepted,
                        "needs_review": s.needs_review, "not_found": s.not_found,
                        "no_synced": s.no_synced, "skipped_cached": s.skipped_cached,
                        "skipped_satisfied": s.skipped_satisfied,
                        "ai_accepted": s.ai_accepted, "ai_suggested": s.ai_suggested,
                        "instrumental_placeholder": s.instrumental_placeholder,
                        "provider_disabled": s.provider_disabled,
                        "errors": s.errors[:20],
                    }
                    if apply_now:
                        progress("抓取完成，开始写入…")
                        # 写入**只作用于本次抓取处理过的曲目**。
                        # 否则 limit 只限制抓取、写入却遍历全部待写入项——
                        # 传 limit=5 却对整库动手，实际遇到过这个语义不一致。
                        apply_paths = set(paths) if paths else set(s.considered_paths)
                        a = run_apply(
                            state, cfg, root,
                            only_rel_paths=apply_paths or None,
                            progress=progress,
                        )
                        entry["applied"] = {
                            "considered": a.considered, "written": a.applied,
                            "skipped_satisfied": a.skipped_satisfied,
                            "failed": a.apply_failed, "deferred_locked": a.deferred_locked,
                            "errors": a.errors[:20],
                        }
                    out.append(entry)
            finally:
                for provider in providers:
                    provider.close()
            return {"roots": out}

        return runner.start("fetch", job).to_dict()

    # ------------------------------------------------------------------
    # 重构：待办收件箱 / 任务明细 / 来源健康 / 入库流水线
    # ------------------------------------------------------------------

    _TODO_KINDS = ("lyrics", "tags", "covers", "organize", "orphans", "failed")

    def _todo_counts() -> dict[str, int]:
        """六类待办的计数。全部走现有表，惰性聚合，不做全库大查询。"""
        lyrics = int(state.conn.execute(
            "SELECT COUNT(*) FROM staged_lyrics WHERE state='needs_review'"
        ).fetchone()[0])
        tags = int(state.conn.execute(
            "SELECT COUNT(*) FROM tag_proposals "
            # unmatched 不计入待办：MusicBrainz 匹配不上（如华语专辑覆盖有限），
            # 人工也无从处理（除手动链接指定），留在列表里只是刷屏。
            "WHERE state IN ('auto','review') AND dismissed_at IS NULL"
        ).fetchone()[0])
        covers = int(state.conn.execute(
            """SELECT COUNT(*) FROM lyric_observations obs
               JOIN locations loc ON loc.id = obs.location_id
               WHERE obs.id IN (SELECT MAX(id) FROM lyric_observations GROUP BY location_id)
                 AND loc.missing_since IS NULL
                 AND EXISTS (SELECT 1 FROM json_each(obs.issues)
                             WHERE json_each.value IN ('no_embedded_cover','no_cover_artwork'))"""
        ).fetchone()[0])
        orphans = 0
        try:
            orphans = int(_orphans_count())
        except Exception:  # noqa: BLE001 — 计数失败不阻塞其它队列
            orphans = 0
        failed_staged = int(state.conn.execute(
            "SELECT COUNT(*) FROM staged_lyrics WHERE state='failed'"
        ).fetchone()[0])
        failed_steps = int(state.conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE state='failed'"
        ).fetchone()[0])
        return {
            "lyrics": lyrics,
            "tags": tags,
            "covers": covers,
            # 归置建议的权威视图是整理页预览（organize/preview）；
            # 这里只计任务明细里的归位冲突，避免对全库逐文件算路径
            "organize": int(state.conn.execute(
                "SELECT COUNT(*) FROM task_steps WHERE kind='organize' AND state='review'"
            ).fetchone()[0]),
            "orphans": orphans,
            "failed": failed_staged + failed_steps,
        }

    def _orphans_count() -> int:
        from .audit import find_orphan_sidecars

        total = 0
        for root_cfg in cfg.enabled_roots:
            try:
                total += len(find_orphan_sidecars(root_cfg))
            except Exception:  # noqa: BLE001
                continue
        return total

    @app.get("/api/v1/todo", dependencies=[Depends(require_auth)])
    def todo_view(
        kind: str = "",
        page: int = 1,
        size: int = Query(default=50, le=200),
    ) -> dict[str, Any]:
        """统一待办收件箱。counts 全量返回；items 按类别分页。

        六类队列的数据源：歌词=staged（needs_review）、标签=tag_proposals、
        封面=审计问题码+任务明细、归置=任务明细的归位冲突、孤儿=find_orphan_sidecars、
        失败=写入失败+任务明细失败。点开任何一项，前端用同一个详情抽屉展示。
        """
        counts = _todo_counts()
        page = max(1, page)
        size = max(1, size)
        items: list[dict[str, Any]] = []
        total = 0
        if kind == "lyrics":
            # 单态查询：两态混查再在 Python 里过滤会让分页窗口被另一态占坑，
            # 部分条目任何页都翻不到
            rows = state.conn.execute(
                "SELECT s.id, s.state, s.provider, s.provider_id, s.score, s.ai_json, "
                "l.rel_path, r.name AS root_name "
                "FROM staged_lyrics s JOIN locations l ON l.id = s.location_id "
                "JOIN roots r ON r.id = l.root_id "
                "WHERE s.state = 'needs_review' "
                "ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            ).fetchall()
            total = counts["lyrics"]
            for row in rows:
                ai = None
                if row["ai_json"]:
                    try:
                        ai = json.loads(row["ai_json"])
                    except ValueError:
                        ai = None
                items.append({
                    "kind": kind,
                    "ref": f"staged:{row['id']}",
                    "root": row["root_name"],
                    "rel_path": row["rel_path"],
                    "provider": row["provider"],
                    "score": row["score"],
                    "state": row["state"],
                    "ai": ai,
                })
        elif kind == "failed":
            # 两类来源合并：写入失败的暂存行 + 任务步骤失败（封面/归位等）
            staged_rows = state.conn.execute(
                "SELECT s.id, s.provider, s.score, l.rel_path, r.name AS root_name "
                "FROM staged_lyrics s JOIN locations l ON l.id = s.location_id "
                "JOIN roots r ON r.id = l.root_id "
                "WHERE s.state = 'failed' ORDER BY s.created_at DESC",
            ).fetchall()
            for row in staged_rows:
                items.append({
                    "kind": "failed",
                    "ref": f"staged:{row['id']}",
                    "root": row["root_name"],
                    "rel_path": row["rel_path"],
                    "provider": row["provider"],
                    "score": row["score"],
                    "state": "failed",
                    "source": "写入失败",
                })
            for step_row in state.failed_task_steps(limit=500):
                detail = json.loads(step_row["detail_json"] or "{}")
                items.append({
                    "kind": "failed",
                    "ref": f"step:{step_row['task_id']}:{step_row['seq']}",
                    "root": step_row.get("root_name") or "",
                    "rel_path": step_row["rel_path"] or "",
                    "state": "failed",
                    "source": f"任务步骤失败（{step_row['kind']}）",
                    "detail": detail,
                })
            total = len(items)
            items = items[(page - 1) * size : (page - 1) * size + size]
        elif kind == "tags":
            rows = state.conn.execute(
                "SELECT p.id, p.state, p.source, p.confidence, p.title, p.artists, "
                "p.album, l.rel_path, r.name AS root_name "
                "FROM tag_proposals p JOIN locations l ON l.id = p.location_id "
                "JOIN roots r ON r.id = l.root_id "
                "WHERE p.state IN ('auto','review') "
                "AND p.dismissed_at IS NULL "
                "ORDER BY p.updated_at DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            ).fetchall()
            total = counts["tags"]
            for row in rows:
                items.append({
                    "kind": "tags",
                    "ref": f"proposal:{row['id']}",
                    "root": row["root_name"],
                    "rel_path": row["rel_path"],
                    "title": row["title"],
                    "artists": row["artists"],
                    "album": row["album"],
                    "state": row["state"],
                    "source": row["source"],
                    "confidence": row["confidence"],
                })
        elif kind == "covers":
            rows = state.conn.execute(
                """SELECT loc.rel_path, ro.name AS root_name, obs.issues
                   FROM lyric_observations obs
                   JOIN locations loc ON loc.id = obs.location_id
                   JOIN roots ro ON ro.id = loc.root_id
                   WHERE obs.id IN (SELECT MAX(id) FROM lyric_observations GROUP BY location_id)
                     AND loc.missing_since IS NULL
                     AND EXISTS (SELECT 1 FROM json_each(obs.issues)
                                 WHERE json_each.value IN ('no_embedded_cover','no_cover_artwork'))
                   ORDER BY loc.rel_path LIMIT ? OFFSET ?""",
                (size, (page - 1) * size),
            ).fetchall()
            total = counts["covers"]
            for row in rows:
                items.append({
                    "kind": "covers",
                    "ref": f"cover:{row['root_name']}:{row['rel_path']}",
                    "root": row["root_name"],
                    "rel_path": row["rel_path"],
                    "issues": json.loads(row["issues"] or "[]"),
                })
        elif kind == "organize":
            rows = state.conn.execute(
                "SELECT s.rel_path, s.detail_json, t.started_at, r.name AS root_name "
                "FROM task_steps s JOIN tasks t ON t.id = s.task_id "
                "JOIN locations l ON l.rel_path = s.rel_path "
                "JOIN roots r ON r.id = l.root_id "
                "WHERE s.kind='organize' AND s.state='review' "
                "ORDER BY t.started_at DESC LIMIT ? OFFSET ?",
                (size, (page - 1) * size),
            ).fetchall()
            total = counts["organize"]
            for row in rows:
                items.append({
                    "kind": "organize",
                    "ref": f"organize:{row['rel_path']}",
                    "root": row["root_name"],
                    "rel_path": row["rel_path"],
                    "detail": json.loads(row["detail_json"] or "{}"),
                })
        elif kind == "orphans":
            from .audit import find_orphan_sidecars

            for root_cfg in cfg.enabled_roots:
                for entry in find_orphan_sidecars(root_cfg):
                    entry["root"] = root_cfg.name
                    entry["kind"] = "orphans"
                    items.append(entry)
            total = len(items)
            items = items[(page - 1) * size : (page - 1) * size + size]
        elif kind:
            raise HTTPException(
                status_code=400,
                detail=f"未知待办类别：{kind}（可用：{', '.join(_TODO_KINDS)}）",
            )
        return {"counts": counts, "total": total, "page": page, "size": size, "items": items}

    @app.get("/api/v1/runs/{task_id}", dependencies=[Depends(require_auth)])
    def run_detail(task_id: str, step_page: int = 1, step_size: int = Query(default=100, le=500)) -> dict[str, Any]:
        """一次批量任务的明细：任务状态 + 每文件 × 每环节的结果矩阵。"""
        # 先读内存再读库：终态落库是异步的，只查库会把「刚完成」的任务
        # 显示成「还在跑」，轮询界面会一直转圈
        record = runner.get(task_id)
        if record is not None:
            task_view: dict[str, Any] = {
                "id": record.id, "kind": record.kind, "state": record.state,
                "progress": record.progress, "error": record.error,
                "result": record.result,
                "started_at": record.started_at, "finished_at": record.finished_at,
            }
        else:
            task = state.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            task_view = {
                "id": task["id"], "kind": task["kind"], "state": task["state"],
                "progress": task["progress"], "error": task["error"],
                "result": json.loads(task["result_json"]) if task["result_json"] else None,
                "started_at": task["started_at"], "finished_at": task["finished_at"],
            }
        # 分页下沉 SQL：大任务的 steps 上万行，轮询时全量拉取纯属浪费
        start = (max(1, step_page) - 1) * step_size
        total_steps = int(state.conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchone()[0])
        steps = state.conn.execute(
            "SELECT seq, kind, rel_path, state, detail_json FROM task_steps "
            "WHERE task_id=? ORDER BY seq, id LIMIT ? OFFSET ?",
            (task_id, step_size, start),
        ).fetchall()
        return {
            "task": task_view,
            "total_steps": total_steps,
            "steps": [
                {
                    "seq": s["seq"], "kind": s["kind"], "rel_path": s["rel_path"],
                    "state": s["state"], "detail": json.loads(s["detail_json"] or "{}"),
                }
                for s in steps
            ],
        }

    @app.get("/api/v1/providers/health", dependencies=[Depends(require_auth)])
    def providers_health() -> dict[str, Any]:
        """非官方来源的健康面板：30 天成功率、风控/结构变更计数。"""
        return {"days": 30, "providers": state.provider_health_summary(days=30)}

    @app.post("/api/v1/tasks/ingest", dependencies=[Depends(require_auth)])
    def start_ingest(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """手动重跑入库流水线（审计→身份→歌词→封面→归位）。

        范围与刮削任务一致：root + dir/paths。范围缺省 = 全库。
        """
        payload = payload or {}
        root_name = payload.get("root")
        raw_paths = payload.get("paths") or []
        paths = {str(p) for p in raw_paths if str(p).strip()} or None
        dir_prefix = str(payload.get("dir") or "").strip().strip("/")
        if ".." in dir_prefix.split("/"):
            raise HTTPException(status_code=400, detail="目录参数不能包含 ..")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        from .ingest import run_ingest
        from .providers import create_providers

        def _scope_paths(root_cfg: RootConfig) -> set[str] | None:
            from . import scan

            if paths:
                return paths
            base = root_cfg.resolved_path
            target = (base / dir_prefix).resolve() if dir_prefix else base
            if target != base and base not in target.parents:
                raise HTTPException(status_code=400, detail="目录不在受管根内")
            if not target.is_dir():
                raise HTTPException(status_code=404, detail=f"目录不存在：{dir_prefix}")
            return {
                str(path.relative_to(base))
                for path in scan.iter_audio_files(target, root_cfg)
            }

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            with _manual_lease(state, wait_seconds=120, label="manual-ingest") as lease:
                if not lease["acquired"]:
                    raise RuntimeError(f"写入任务仍在运行（{lease['holder']}），请稍后重试")
                providers = create_providers(cfg.providers)
                out: list[dict[str, Any]] = []
                try:
                    for root_cfg in roots:
                        scope = _scope_paths(root_cfg)
                        rels = sorted(scope) if scope is not None else []
                        if not rels:
                            out.append({"root": root_cfg.name, "processed": 0,
                                        "note": "该目录下没有音频文件"})
                            continue
                        truncated = len(rels) > 2000
                        result = run_ingest(
                            state, cfg, root_cfg, providers,
                            rel_paths=rels[:2000], progress=progress,
                        )
                        out.append({"root": root_cfg.name, **{
                            k: v for k, v in result.items() if k != "task_id"
                        }, "task_id": result.get("task_id"),
                           "truncated": truncated,
                           "total_files": len(rels)})
                finally:
                    for provider in providers:
                        provider.close()
                return {"roots": out}

        return runner.start("ingest", job).to_dict()

    @app.post("/api/v1/tasks/ai-pre-review", dependencies=[Depends(require_auth)])
    def start_ai_pre_review(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """对待审查队列做一次模型判定，高置信条目转为待写入。

        这是「减少人工干预」的主通道：抓取时逐首判定会被大批量扫描摊薄，
        这里对**已在队列里的**条目集中判定，预算用完为止。
        """
        payload = payload or {}
        root_name = payload.get("root")
        limit = int(payload.get("limit") or 200)
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        if not cfg.ai.enabled:
            raise HTTPException(status_code=409, detail="大模型功能未启用")
        if not cfg.ai.pre_review:
            raise HTTPException(status_code=409, detail="智能预审未启用")

        from .audit import audit_root
        from .match import build_context
        from .pipeline import sync_state
        from .pre_review import run_ai_pre_review
        from .providers import create_providers

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            providers = create_providers(cfg.providers)
            out: list[dict[str, Any]] = []
            try:
                for root_cfg in roots:
                    if root_cfg.read_only:
                        continue
                    audit = audit_root(root_cfg)
                    sync_state(state, cfg, root_cfg, audit, full_scan=False)
                    root_id = state.get_root_id(root_cfg.name) or -1
                    contexts: dict[int, tuple[Any, str]] = {}
                    for track in audit.tracks:
                        loc = state.location_by_rel_path(
                            root_id, str(track.audio.rel_path)
                        )
                        if loc is None:
                            continue
                        ctx = build_context(track.tags, track.audio.duration)
                        contexts[int(loc["id"])] = (ctx, "")
                    if not contexts:
                        continue
                    provider_map = {p.name: p for p in providers}
                    result = run_ai_pre_review(
                        state, cfg, root_cfg, provider_map, contexts,
                        limit=limit, progress=progress,
                    )
                    out.append({"root": root_cfg.name, **result.to_dict()})
                return {"roots": out}
            finally:
                for provider in providers:
                    provider.close()

        return runner.start("ai-pre-review", job).to_dict()

    @app.post("/api/v1/tasks/cover-fetch", dependencies=[Depends(require_auth)])
    def start_cover_fetch(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """按目录批量补封面：同目录图 + CAA（按专辑聚合，一次下载逐文件嵌入）。"""
        payload = payload or {}
        root_name = payload.get("root")
        apply_now = bool(payload.get("apply"))
        if apply_now and not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409, detail="配置里写入开关已关闭"
            )
        dir_prefix = str(payload.get("dir") or "").strip().strip("/")
        if ".." in dir_prefix.split("/"):
            raise HTTPException(status_code=400, detail="目录参数不能包含 ..")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        from . import scan as scan_mod
        from .ingest import run_cover_stage

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            with _manual_lease(state, wait_seconds=120, label="manual-cover") as lease:
                if not lease["acquired"]:
                    raise RuntimeError(f"写入任务仍在运行（{lease['holder']}），请稍后重试")
                saved_auto = cfg.policy.cover.auto_write
                try:
                    cfg.policy.cover.auto_write = saved_auto or apply_now
                    out: list[dict[str, Any]] = []
                    for root_cfg in roots:
                        base = root_cfg.resolved_path
                        target = (base / dir_prefix).resolve() if dir_prefix else base
                        if target != base and base not in target.parents:
                            raise HTTPException(status_code=400, detail="目录不在受管根内")
                        if not target.is_dir():
                            raise HTTPException(
                                status_code=404, detail=f"目录不存在：{dir_prefix}"
                            )
                        rels = sorted(
                            str(path.relative_to(base))
                            for path in scan_mod.iter_audio_files(target, root_cfg)
                        )
                        progress(f"{root_cfg.name}: 处理 {len(rels)} 个文件")
                        result = run_cover_stage(state, cfg, root_cfg, rel_paths=rels)
                        out.append({"root": root_cfg.name, **result})
                    return {"roots": out}
                finally:
                    cfg.policy.cover.auto_write = saved_auto

        return runner.start("cover-fetch", job).to_dict()

    def _cover_settings_view() -> dict[str, Any]:
        return {
            "policy": asdict(cfg.policy.cover),
            "revision": state.get_meta("settings.revision") or "",
        }

    @app.get("/api/v1/settings/cover", dependencies=[Depends(require_auth)])
    def cover_settings_view() -> dict[str, Any]:
        return _cover_settings_view()

    @app.put("/api/v1/settings/cover", dependencies=[Depends(require_auth)])
    def update_cover_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        try:
            apply_cover_policy_values(cfg, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.set_meta("settings.cover_policy", json.dumps(payload, ensure_ascii=False))
        revision = str(time.time_ns())
        state.set_meta("settings.revision", revision)
        _invalidate_caches()
        return {"ok": True, "policy": asdict(cfg.policy.cover), "revision": revision}

    def _organize_settings_view() -> dict[str, Any]:
        return {
            "policy": asdict(cfg.policy.organize),
            "revision": state.get_meta("settings.revision") or "",
        }

    @app.get("/api/v1/settings/organize", dependencies=[Depends(require_auth)])
    def organize_settings_view() -> dict[str, Any]:
        return _organize_settings_view()

    @app.put("/api/v1/settings/organize", dependencies=[Depends(require_auth)])
    def update_organize_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        try:
            apply_organize_policy_values(cfg, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.set_meta("settings.organize_policy", json.dumps(payload, ensure_ascii=False))
        revision = str(time.time_ns())
        state.set_meta("settings.revision", revision)
        _invalidate_caches()
        return {"ok": True, "policy": asdict(cfg.policy.organize), "revision": revision}

    def _lyrics_settings_view() -> dict[str, Any]:
        return {
            "settings": asdict(cfg.lyrics),
            "revision": state.get_meta("settings.revision") or "",
        }

    @app.get("/api/v1/settings/lyrics", dependencies=[Depends(require_auth)])
    def lyrics_settings_view() -> dict[str, Any]:
        return _lyrics_settings_view()

    @app.put("/api/v1/settings/lyrics", dependencies=[Depends(require_auth)])
    def update_lyrics_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        try:
            apply_lyrics_settings_values(cfg, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.set_meta("settings.lyrics_format", json.dumps(payload, ensure_ascii=False))
        revision = str(time.time_ns())
        state.set_meta("settings.revision", revision)
        _invalidate_caches()
        return {"ok": True, "settings": asdict(cfg.lyrics), "revision": revision}

    @app.get("/api/v1/settings/acoustid", dependencies=[Depends(require_auth)])
    def acoustid_settings_view() -> dict[str, Any]:
        key = cfg.providers.acoustid.api_key
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("已配置" if key else "")
        return {
            "enabled": cfg.providers.acoustid.enabled,
            "has_key": bool(key),
            "masked": masked,
            "source": "env" if os.environ.get("LYRA_ACOUSTID_API_KEY") else (
                "settings" if state.get_meta("settings.acoustid_key") else "none"
            ),
        }

    @app.put("/api/v1/settings/acoustid", dependencies=[Depends(require_auth)])
    def update_acoustid_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置必须是对象")
        if os.environ.get("LYRA_ACOUSTID_API_KEY"):
            raise HTTPException(
                status_code=409,
                detail="环境变量 LYRA_ACOUSTID_API_KEY 已设置且优先，无需在此保存",
            )
        api_key = payload.get("api_key")
        try:
            apply_acoustid_key(cfg, api_key if isinstance(api_key, str) else "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.set_meta("settings.acoustid_key", cfg.providers.acoustid.api_key)
        state.set_meta(
            "settings.acoustid_enabled", "1" if payload.get("enabled") else "0"
        )
        cfg.providers.acoustid.enabled = bool(payload.get("enabled"))
        revision = str(time.time_ns())
        state.set_meta("settings.revision", revision)
        _invalidate_caches()
        return {"ok": True, "has_key": bool(cfg.providers.acoustid.api_key)}

    @app.post("/api/v1/tasks/tags-audit", dependencies=[Depends(require_auth)])
    def start_tags_audit(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """批量标签审计：扫全库，统计 MusicBrainz 能补什么、冲突在哪。

        **只读，不写入任何文件。** 先出一份可核对的账，再决定是否落盘——
        标签写入的影响面比歌词大，盲写只会制造一堆错误。
        """
        payload = payload or {}
        limit = payload.get("limit")
        root_name = payload.get("root")
        mb_cfg = cfg.providers.musicbrainz
        if not mb_cfg.enabled or not mb_cfg.base_url.strip():
            raise HTTPException(
                status_code=503,
                detail="未配置 MusicBrainz（providers.musicbrainz.base_url）",
            )
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        from .tagaudit import audit_tags

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            provider = MusicBrainzProvider(
                MusicBrainzConfig(
                    base_url=mb_cfg.base_url,
                    request_timeout_seconds=mb_cfg.request_timeout_seconds,
                    minimum_request_interval_seconds=mb_cfg.minimum_request_interval_seconds,
                    search_limit=mb_cfg.search_limit,
                    detail_limit=mb_cfg.detail_limit,
                )
            )
            try:
                out = []
                for root in roots:
                    result = audit_tags(
                        cfg, root, provider,
                        progress=progress,
                        limit=int(limit) if limit else None,
                    )
                    out.append({"root": root.name, **result.to_dict()})
                return {"roots": out}
            finally:
                provider.close()

        return runner.start("tags-audit", job).to_dict()

    # -----------------------------------------------------------------------
    # 标签刮削
    # -----------------------------------------------------------------------

    @app.post("/api/v1/tasks/tags-scan", dependencies=[Depends(require_auth)])
    def start_tags_scan(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """扫描全库并生成逐字段标签提案；可按策略自动写入「补空」部分。"""
        payload = payload or {}
        mb_cfg = cfg.providers.musicbrainz
        if not mb_cfg.enabled or not mb_cfg.base_url.strip():
            raise HTTPException(
                status_code=503,
                detail="未配置 MusicBrainz（providers.musicbrainz.base_url）",
            )
        root_name = str(payload.get("root") or "")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        raw_auto = payload.get("auto_write")
        auto_write = (
            cfg.policy.tags.auto_write if raw_auto is None else bool(raw_auto)
        )
        use_fingerprint = (
            cfg.policy.tags.fingerprint
            if payload.get("fingerprint") is None
            else bool(payload.get("fingerprint"))
        )
        limit = payload.get("limit")
        limit_value = int(limit) if limit else None

        from .fingerprint import AcoustIdClient, FpcalcFingerprinter
        from .tagscrape import scan_tags

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            from .providers import create_providers

            provider = _make_mb_provider(cfg)
            # 歌词来源作为标签兜底（MusicBrainz 覆盖不到的华语专辑）
            lyric_providers = create_providers(cfg.providers)
            fingerprinter = FpcalcFingerprinter() if use_fingerprint else None
            acoustid = (
                AcoustIdClient(cfg.providers.acoustid)
                if use_fingerprint and cfg.providers.acoustid.enabled
                else None
            )
            try:
                out: list[dict[str, Any]] = []
                for root in roots:
                    progress(f"正在扫描 {root.name}…")
                    result = scan_tags(
                        cfg,
                        root,
                        state,
                        provider,
                        lyric_providers=lyric_providers,
                        auto_write=auto_write,
                        limit=limit_value,
                        progress=progress,
                        fingerprinter=fingerprinter,
                        acoustid=acoustid,
                    )
                    out.append({"root": root.name, **result.to_dict()})
                if (
                    limit_value is None
                    and out
                    and all(item.get("scanned") for item in out)
                ):
                    # 限量扫描只用于试跑，不更新自动调度的时间戳。
                    state.set_meta("tags.last_scan_at", str(time.time()))
                    state.set_meta(
                        "tags.last_scan_summary",
                        json.dumps({"roots": out}, ensure_ascii=False),
                    )
                _invalidate_caches()
                return {"roots": out}
            finally:
                provider.close()
                if acoustid is not None:
                    acoustid.close()

        return runner.start("tags-scan", job).to_dict()

    @app.get("/api/v1/tags/summary", dependencies=[Depends(require_auth)])
    def tags_summary() -> dict[str, Any]:
        """标签刮削的当前状态：待处理数量、上次扫描结果、来源可用性。"""
        from .fingerprint import FpcalcFingerprinter

        counts = state.tag_proposal_counts()
        last_at = state.get_meta("tags.last_scan_at")
        raw_last = state.get_meta("tags.last_scan_summary")
        last_scan: dict[str, Any] | None = None
        if raw_last:
            try:
                parsed = json.loads(str(raw_last))
                last_scan = parsed if isinstance(parsed, dict) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                last_scan = None
        mb_cfg = cfg.providers.musicbrainz
        ac_cfg = cfg.providers.acoustid
        queue = {
            "auto": counts.get("auto", 0),
            "review": counts.get("review", 0),
            "unmatched": counts.get("unmatched", 0),
        }
        return {
            "counts": {
                **queue,
                # 已忽略不算待办，单列出来给人看「我忽略过什么」。
                "ignored": counts.get("ignored", 0),
            },
            # 待办总数只算需要人决策的（auto 待写 + review 待确认）；
            # unmatched 是「来源匹配不上」，人工同样无从下手，不占待办
            "total": queue["auto"] + queue["review"],
            "last_scan_at": float(last_at) if last_at else None,
            "last_scan": last_scan,
            "write_enabled": cfg.policy.write_enabled,
            "source": {
                "musicbrainz": bool(mb_cfg.enabled and mb_cfg.base_url.strip()),
                "acoustid": bool(
                    ac_cfg.enabled and ac_cfg.api_key.strip()
                ),
                "fingerprint_tool": FpcalcFingerprinter().available,
            },
            "policy": {
                "auto_scan": cfg.policy.tags.auto_scan,
                "scan_interval_hours": cfg.policy.tags.scan_interval_hours,
                "auto_write": cfg.policy.tags.auto_write,
                "fingerprint": cfg.policy.tags.fingerprint,
                "max_auto_write_per_run": cfg.policy.tags.max_auto_write_per_run,
                "max_duration_delta_seconds": (
                    cfg.policy.tags.max_duration_delta_seconds
                ),
            },
        }

    @app.get("/api/v1/tags/proposals", dependencies=[Depends(require_auth)])
    def list_tag_proposals(
        state_filter: str = Query("", alias="state"),
        q: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页列出提案。按状态分桶：auto / review / unmatched / ignored。"""
        wanted = state_filter.strip()
        if wanted and wanted not in ("auto", "review", "unmatched", "ignored"):
            raise HTTPException(status_code=400, detail="未知的标签提案状态")
        size = max(1, min(200, int(limit)))
        start = max(0, int(offset))
        items = [
            _tag_proposal_dict(row, cfg)
            for row in state.iter_tag_proposals(
                state=wanted or None, query=q, limit=size, offset=start
            )
        ]
        return {
            "total": state.count_tag_proposals(state=wanted or None, query=q),
            "items": items,
            "limit": size,
            "offset": start,
        }

    @app.post("/api/v1/tags/proposals/{proposal_id}/apply", dependencies=[Depends(require_auth)])
    @manual_lease()
    def apply_tag_proposal(
        proposal_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """写入一条提案里勾选的字段。"""
        from .tagwrite import apply_proposal

        payload = payload or {}
        row = state.tag_proposal(proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这条标签提案不存在")
        raw_fields = payload.get("fields")
        if raw_fields is not None and not isinstance(raw_fields, list):
            raise HTTPException(status_code=400, detail="fields 必须是数组")
        fields = [str(item) for item in raw_fields] if raw_fields is not None else None
        root = _select_root(cfg, str(row["root_name"]))
        outcome = apply_proposal(state, cfg, root, row, fields=fields)
        if not outcome.ok:
            raise HTTPException(status_code=422, detail=outcome.error or "写入失败")
        _refresh_observation(root, str(row["rel_path"]))
        _invalidate_caches()
        return {
            "ok": True,
            "rel_path": outcome.rel_path,
            "applied": list(outcome.applied.keys()),
            "values": outcome.applied,
            "remaining": outcome.remaining,
        }

    @app.post("/api/v1/tags/proposals/{proposal_id}/ignore", dependencies=[Depends(require_auth)])
    def ignore_tag_proposal(proposal_id: int) -> dict[str, Any]:
        """人工忽略一条提案：出队，并且**不再被扫描翻出来**。

        与「暂时跳过」的区别：忽略会记在提案上，自动扫描认得它，
        下一轮不会又把这首曲子提回来。可以在「已忽略」里恢复。
        """
        row = state.tag_proposal(proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这条标签提案不存在")
        state.dismiss_tag_proposal(proposal_id)
        return {"ok": True}

    @app.post("/api/v1/tags/proposals/{proposal_id}/restore", dependencies=[Depends(require_auth)])
    def restore_tag_proposal(proposal_id: int) -> dict[str, Any]:
        """恢复一条被忽略的提案，重新回到待审查 / 未匹配队列。"""
        row = state.tag_proposal(proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这条标签提案不存在")
        if str(row["state"]) != "ignored":
            raise HTTPException(status_code=400, detail="这条提案没有被忽略")
        state.restore_tag_proposal(proposal_id)
        return {"ok": True}

    @app.post("/api/v1/tags/proposals/bulk-apply", dependencies=[Depends(require_auth)])
    @manual_lease()
    def bulk_apply_tag_proposals(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """批量写入。

        - `scope=auto`：所有高置信、无冲突的自动项；
        - `scope=fills`：给定 id 里只写「补空」字段（冲突留给人）；
        - `scope=recommended`：给定 id 里按建议写入，含冲突——
        人工逐行勾选后批量应用，属于显式确认，与单条展开写入同口径。
        """
        from .tagwrite import apply_proposal

        payload = payload or {}
        raw_ids = payload.get("ids") or []
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="ids 必须是数组")
        try:
            ids = [int(item) for item in raw_ids]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="ids 必须是整数数组") from exc
        scope = str(payload.get("scope") or ("fills" if ids else "auto"))
        if scope not in ("auto", "fills", "recommended"):
            raise HTTPException(status_code=400, detail="未知的批量写入范围")

        if scope == "auto":
            rows = state.iter_tag_proposals(
                state="auto", limit=max(1, cfg.policy.tags.max_auto_write_per_run)
            )
        elif ids:
            rows = [
                row for row in (state.tag_proposal(item) for item in ids)
                if row is not None
            ]
            if not payload.get("include_search"):
                # 标题搜索结果可能整条都错；批量补空不能替人做这个判断。
                filtered = []
                for row in rows:
                    try:
                        match = json.loads(str(row["payload_json"])).get("match") or {}
                    except (TypeError, ValueError, json.JSONDecodeError):
                        filtered.append(row)
                        continue
                    if str(match.get("source") or "") != "search":
                        filtered.append(row)
                rows = filtered
        else:
            raise HTTPException(status_code=400, detail="没有指定要写入的提案")

        applied_files = 0
        applied_fields = 0
        errors: list[str] = []
        for row in rows:
            try:
                root = _select_root(cfg, str(row["root_name"]))
                fields = None
                if scope == "recommended":
                    fields = _recommended_fields(row)
                    if not fields:
                        continue
                outcome = apply_proposal(state, cfg, root, row, fields=fields)
            except Exception as exc:  # noqa: BLE001 — 单条失败不影响整批
                errors.append(f"{row['rel_path']}: {type(exc).__name__}: {exc}")
                continue
            if outcome.ok:
                applied_files += 1
                applied_fields += len(outcome.applied)
                _refresh_observation(root, str(row["rel_path"]))
            else:
                errors.append(f"{row['rel_path']}: {outcome.error}")
        _invalidate_caches()
        return {
            "ok": True,
            "files": applied_files,
            "fields": applied_fields,
            "errors": errors[:30],
        }

    @app.post("/api/v1/tags/proposals/{proposal_id}/link", dependencies=[Depends(require_auth)])
    def link_tag_proposal(
        proposal_id: int, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """按 MusicBrainz 链接重新对照。

        不传 `index` 时只返回候选预览，由界面选择；传了才真正替换提案。
        链接是使用者的明确判断，但仍然走逐字段勾选，不整表覆盖。
        """
        from .tagscrape import (
            SOURCE_LINK,
            MatchResult,
            proposal_payload_from_match,
            snapshot_for,
        )

        payload = payload or {}
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="没有填入链接")
        row = state.tag_proposal(proposal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这条标签提案不存在")
        root = _select_root(cfg, str(row["root_name"]))
        try:
            link = parse_entity_link(url)
        except MbidLinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        target = root.resolved_path / str(row["rel_path"])
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        meta = read_audio_meta(target, root.resolved_path)
        all_tags, tags_error = read_all_tags(target)
        if tags_error:
            raise HTTPException(status_code=422, detail=tags_error)
        context = build_context(meta.tags, meta.info.duration)

        provider = _make_mb_provider(cfg)
        try:
            candidates = lookup_by_link(
                provider,
                link,
                title=context.title,
                duration=context.duration,
                album=context.album,
                album_id=str((all_tags.get("MUSICBRAINZ_ALBUMID") or [""])[0]),
                recording_id=str((all_tags.get("MUSICBRAINZ_TRACKID") or [""])[0]),
                release_track_id=str(
                    (all_tags.get("MUSICBRAINZ_RELEASETRACKID") or [""])[0]
                ),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"MusicBrainz 请求失败：{exc}"
            ) from exc
        finally:
            provider.close()

        if not candidates:
            raise HTTPException(
                status_code=404, detail="链接有效，但没有取到可用的标签字段"
            )

        previews = []
        for index, candidate in enumerate(candidates[:8]):
            delta = None
            if candidate.duration is not None and context.duration is not None:
                delta = abs(candidate.duration - context.duration)
            rows = compare_fields(all_tags, candidate.fields)
            previews.append(
                {
                    "index": index,
                    "title": candidate.title,
                    "artists": list(candidate.artists),
                    "album": candidate.album,
                    "duration": candidate.duration,
                    "duration_delta": delta,
                    "rows": rows,
                    "summary": summarise(rows),
                    "extra": dict(candidate.extra),
                }
            )

        raw_index = payload.get("index")
        if raw_index is None:
            return {
                "link": link.to_dict(),
                "candidates": previews,
                "applied": False,
            }
        try:
            chosen = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="index 必须是整数") from exc
        if chosen < 0 or chosen >= len(previews):
            raise HTTPException(status_code=400, detail="候选序号超出范围")

        candidate = candidates[chosen]
        match = MatchResult(
            source=SOURCE_LINK,
            confidence=1.0,
            release_id=str(
                (candidate.fields.get("MUSICBRAINZ_ALBUMID") or [""])[0]
            ),
            recording_id=str(
                (candidate.fields.get("MUSICBRAINZ_TRACKID") or [""])[0]
            ),
            album=candidate.album,
            duration_delta=previews[chosen]["duration_delta"],
            fields={k: list(v) for k, v in candidate.fields.items()},
            extra={**dict(candidate.extra), "匹配": "按链接指定"},
            notes=(
                ["链接里没有定位到曲目，曲序不会写入"]
                if "TRACKNUMBER" not in candidate.fields
                else []
            ),
        )
        snap = snapshot_for(
            root,
            int(row["location_id"]),
            str(row["rel_path"]),
            target,
            all_tags,
            meta.info.duration,
            meta.info.media_format.value,
            int(meta.info.size or 0),
            int(meta.info.mtime or 0),
        )
        rows, new_payload = proposal_payload_from_match(
            snap, match, source=SOURCE_LINK
        )
        state.upsert_tag_proposal(
            location_id=int(row["location_id"]),
            state="review",
            source=SOURCE_LINK,
            confidence=1.0,
            title=snap.title,
            artists="; ".join(snap.artists),
            album=match.album or snap.album,
            release_id=match.release_id,
            recording_id=match.recording_id,
            duration_delta=match.duration_delta,
            payload=new_payload,
            revive_skipped=True,
        )
        updated = state.tag_proposal(proposal_id)
        return {
            "link": link.to_dict(),
            "candidates": previews,
            "applied": True,
            "proposal": _tag_proposal_dict(updated, cfg) if updated else None,
        }

    @app.get("/api/v1/tags/history", dependencies=[Depends(require_auth)])
    def tag_write_history(limit: int = 100) -> dict[str, Any]:
        """最近写入了哪些标签，以及还能不能撤销。"""
        items = []
        for row in state.iter_tag_writes(limit=min(500, max(1, int(limit)))):
            try:
                applied = json.loads(str(row["applied_json"]))
                before = json.loads(str(row["before_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                applied, before = {}, {}
            items.append(
                {
                    "id": int(row["id"]),
                    "txn_id": str(row["txn_id"]),
                    "rel_path": str(row["rel_path"]),
                    "root": str(row["root_name"]),
                    "applied": applied,
                    "before": before,
                    "written_at": float(row["written_at"]),
                    "undone": bool(row["undone_at"]),
                }
            )
        return {"items": items}

    @app.post("/api/v1/tags/writes/{write_id}/undo", dependencies=[Depends(require_auth)])
    @manual_lease()
    def undo_tag_write_endpoint(write_id: int) -> dict[str, Any]:
        """撤销一次标签写入（只还原那次写入涉及的字段）。"""
        from .tagwrite import undo_tag_write

        row = state.tag_write(write_id)
        if row is None:
            raise HTTPException(status_code=404, detail="这条写入记录不存在")
        root = _select_root(cfg, str(row["root_name"]))
        outcome = undo_tag_write(state, cfg, root, row)
        if not outcome.ok:
            raise HTTPException(status_code=422, detail=outcome.error or "撤销失败")
        _refresh_observation(root, str(row["rel_path"]))
        _invalidate_caches()
        return {
            "ok": True,
            "rel_path": outcome.rel_path,
            "restored": list(outcome.applied.keys()),
            "removed": outcome.removed,
        }

    @app.post("/api/v1/tasks/apply", dependencies=[Depends(require_auth)])
    def start_apply(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="write_enabled=false，全库只读。请先在配置中显式打开。",
            )
        limit = payload.get("limit")
        root_name = payload.get("root")

        from .pipeline import run_apply

        roots = [
            r for r in cfg.enabled_roots
            if (not root_name or r.name == root_name) and not r.read_only
        ]
        if not roots:
            raise HTTPException(status_code=400, detail="没有可写的受管根")

        def job(progress: Callable[[str], None]) -> dict[str, Any]:
            out: list[dict[str, Any]] = []
            # 人工优先：监控看到等待登记会让出（每写完一个文件检查一次）。
            with _manual_lease(state, wait_seconds=120, label="manual-apply") as lease:
                if not lease["acquired"]:
                    holder = lease["holder"] or "其它任务"
                    raise RuntimeError(
                        f"没有拿到写入许可：当前有其它任务正在写入（{holder}）。"
                        "可以稍后重试，或先停止那个任务。"
                    )
                for root in roots:
                    s = run_apply(
                        state, cfg, root, limit=int(limit) if limit else None,
                        progress=progress,
                    )
                    out.append(
                        {
                            "root": root.name, "applied": s.applied,
                            "apply_failed": s.apply_failed,
                            "skipped_satisfied": s.skipped_satisfied,
                            "errors": s.errors[:20],
                        }
                    )
            return {"roots": out}

        return runner.start("apply", job).to_dict()

    @app.get("/api/v1/tasks", dependencies=[Depends(require_auth)])
    def list_tasks(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        """任务历史（最新在前）。

        `limit` 必须真的生效：前端请求的是最近 8 条，而这里原来忽略参数、
        返回全部历史，于是刮削器页的运行状态列表越滚越长，把操作卡挤到
        首屏之外。返回 `total` 让界面能说明「还有多少条在日志里」。
        """
        items = [t.to_dict() for t in runner.list()]
        return {"items": items[:limit], "total": len(items)}

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(require_auth)])
    def get_task(task_id: str) -> dict[str, Any]:
        record = runner.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="不存在")
        return record.to_dict()

    # -- 统计 -------------------------------------------------------------

    @app.get("/api/v1/watch", dependencies=[Depends(require_auth)])
    def watch_status() -> dict[str, Any]:
        """监控进程的活动状态。

        监控在后台自动抓取与写入，但界面上原本看不到任何痕迹——
        使用者无法区分「它正常运行」和「它三天前就挂了」。
        """
        import time as _time

        started = state.get_meta("watch.started_at")
        beat = state.get_meta("watch.heartbeat_at")
        counters: dict[str, Any] = {}
        raw = state.get_meta("watch.counters")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    counters = parsed
            except (TypeError, ValueError):
                counters = {}

        now = _time.time()
        age = (now - float(beat)) if beat else None
        return {
            "running": age is not None,
            "started_at": float(started) if started else None,
            "heartbeat_at": float(beat) if beat else None,
            "heartbeat_age_seconds": age,
            # 心跳超过一轮对账间隔的两倍就认为异常——对账间隔默认 900s，
            # 所以 30 分钟没有任何心跳基本可以确定进程有问题。
            "healthy": age is not None and age < 1800,
            "stage": state.get_meta("watch.stage"),
            "detail": state.get_meta("watch.detail"),
            "counters": counters,
            # 最近活动历史：判断「是否正常」靠的是趋势，不是单次心跳
            "recent": state.recent_watch_events(limit=20),
        }

    @app.get("/api/v1/stats", dependencies=[Depends(require_auth)])
    def stats(refresh: bool = False) -> dict[str, Any]:
        """全库统计。首次调用会做一次只读扫描（本机 205 首约 1 秒），之后走缓存。

        有意用「实时扫描」而不是只查数据库：只有这样，音频真实容器、
        lrc 的实际内容状态、配对与孤儿才是准确的——数据库里记的是处理过程，
        不是曲库现状。
        """
        return _stats(cfg, refresh=refresh)

    # -- 文件浏览与预览 ---------------------------------------------------

    @app.get("/api/v1/library/artists", dependencies=[Depends(require_auth)])
    def library_artists(root: str | None = None) -> dict[str, Any]:
        """列出曲库里的艺人，供「按艺人刮削」筛选。

        读的是**文件标签**而不是目录名：目录名只是整理时的快照，
        标签才是「这首歌的艺人是谁」。一个文件可能有多个艺人
        （合唱、群星），这里按单个艺人分别计数。

        遍历整个曲库要几秒，所以结果单独缓存（与统计同一档 TTL）。
        """
        roots = [r for r in cfg.enabled_roots if not root or r.name == root]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        def compute() -> list[dict[str, Any]]:
            from .audit import audit_root
            from .match import build_context

            counts: dict[str, int] = {}
            for root_cfg in roots:
                for track in audit_root(root_cfg).tracks:
                    context = build_context(track.tags, track.audio.duration)
                    for artist in context.artists:
                        name = artist.strip()
                        if name:
                            counts[name] = counts.get(name, 0) + 1
            return [
                {"name": name, "count": count}
                for name, count in sorted(
                    counts.items(), key=lambda kv: (-kv[1], kv[0].lower())
                )
            ]

        items = _STATS_CACHE.get_or_compute(
            "artists:" + ",".join(r.name for r in roots), compute
        )
        return {"total": len(items), "items": items}

    @app.get("/api/v1/library/dirs", dependencies=[Depends(require_auth)])
    def library_dirs(root: str | None = None) -> dict[str, Any]:
        """列出曲库目录树（每层音频数 / 缺 .lrc 数），供「按目录刮削」选择范围。

        为什么要读**磁盘**而不是状态库：使用者刚拷进来的目录还没被扫描登记，
        状态库里查不到它，而「按目录刮削」恰恰最常用在刚拷完的时候。
        """
        from . import scan

        roots = [r for r in cfg.enabled_roots if not root or r.name == root]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        out: list[dict[str, Any]] = []
        for root_cfg in roots:
            base = root_cfg.resolved_path
            if not base.is_dir():
                continue
            counts: dict[str, list[int]] = {"": [0, 0]}
            for path in scan.iter_audio_files(base, root_cfg):
                try:
                    rel = path.relative_to(base)
                except ValueError:
                    continue
                parts = rel.parts[:-1]
                missing = 0 if path.with_suffix(".lrc").is_file() else 1
                # 每一层都累加：父目录的数字包含全部子孙
                for depth in range(len(parts) + 1):
                    key = "/".join(parts[:depth])
                    slot = counts.setdefault(key, [0, 0])
                    slot[0] += 1
                    slot[1] += missing
            dirs = [
                {
                    "path": key,
                    "name": key.rsplit("/", 1)[-1] if key else root_cfg.name,
                    "depth": 0 if not key else key.count("/") + 1,
                    "audio": value[0],
                    "missing_lrc": value[1],
                }
                for key, value in sorted(counts.items())
            ]
            out.append({"root": root_cfg.name, "path": str(base), "dirs": dirs})
        return {"roots": out}

    @app.get("/api/v1/files", dependencies=[Depends(require_auth)])
    def list_files(
        root: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        page: int = 1,
        size: int = Query(default=100, le=2000),
    ) -> dict[str, Any]:
        return _list_files(cfg, root=root, kind=kind, q=q, page=page, size=size)

    @app.post("/api/v1/orphans/clean", dependencies=[Depends(require_auth)])
    @manual_lease()
    def clean_orphans(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """清理「确实多余」的孤儿 .lrc（移入回收站，可还原）。

        - 内容与现有 .lrc 重复（歌词行 100% 被覆盖）
        - 整个曲库里没有对应音频的

        `dry_run=true`（默认）只列清单不动文件。
        """
        payload = payload or {}
        dry_run = payload.get("dry_run", True)
        if not dry_run and not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")
        from .pipeline import clean_orphan_sidecars

        root_name = str(payload.get("root") or "")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        out = []
        for root_cfg in roots:
            out.append({"root": root_cfg.name,
                        **clean_orphan_sidecars(state, cfg, root_cfg,
                                                dry_run=bool(dry_run))})
        if not dry_run:
            _invalidate_caches()
        return {"roots": out, "dry_run": bool(dry_run)}

    @app.post("/api/v1/orphans/ai-review", dependencies=[Depends(require_auth)])
    @manual_lease(wait_seconds=60)
    def ai_review_orphans(payload: dict[str, Any]) -> dict[str, Any]:
        """让 AI 判断「多余的那份歌词」与现有 .lrc 是不是同一首歌。

        只处理 `duplicate`（音频已经有 .lrc）。返回每项的判断与建议动作：
        `cleanup`（多余那份移入回收站）/ `replace`（用多余那份替换）/
        `keep`（判断不是同一首或拿不准，不动）。

        `apply=true` 时按建议执行，但**只在高置信度**下动手；且动作是移入
        回收站（可还原），不是删除。
        """
        if not cfg.ai.enabled or not cfg.ai.api_key.strip():
            raise HTTPException(
                status_code=503,
                detail="未启用 AI（配置 [ai].enabled 或环境变量 LYRA_AI_API_KEY）",
            )
        root_name = str(payload.get("root") or "")
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        raw_paths = payload.get("paths") or []
        wanted = {str(p) for p in raw_paths if str(p).strip()} or None
        apply_now = bool(payload.get("apply"))
        min_conf = float(payload.get("min_confidence") or 0.8)
        if apply_now and not cfg.policy.write_enabled:
            raise HTTPException(status_code=409, detail="写入开关已关闭")

        from .ai import AIError, review_duplicate
        from .audit import find_orphan_sidecars
        from .formats import read_audio_meta

        results: list[dict[str, Any]] = []
        budget = max(0, int(cfg.ai.max_requests_per_run))
        for root_cfg in roots:
            base = root_cfg.resolved_path
            for item in find_orphan_sidecars(root_cfg):
                if item["action"] != "duplicate" or not item.get("paired_sidecar"):
                    continue
                if wanted is not None and item["rel_path"] not in wanted:
                    continue
                if budget <= 0:
                    results.append({"rel_path": item["rel_path"],
                                    "error": "本轮 AI 调用次数已达上限"})
                    continue
                audio_rel = item.get("guess_audio")
                if not audio_rel:
                    results.append({"rel_path": item["rel_path"], "error": "没有候选音频"})
                    continue
                audio_path = base / audio_rel
                try:
                    # **必须按歌词文件的真实编码解码。**
                    # 下载来的 .lrc 常是 GBK；直接 `utf-8 + errors=replace`
                    # 会把正文变成 `�ʣ�С��` 这种乱码交给模型——
                    # 模型拿到乱码仍会一本正经地判断，最后把好文件删了。
                    # 实际：两份 30 行的歌词，用 utf-8 读出来"共有行 0"，
                    # 按真实编码读出来"共有 24 行"。
                    orphan_text = decode_lyrics_bytes(
                        (base / item["rel_path"]).read_bytes())[0]
                    existing_text = decode_lyrics_bytes(
                        (base / item["paired_sidecar"]).read_bytes())[0]
                    meta = read_audio_meta(audio_path, base)
                except OSError as exc:
                    results.append({"rel_path": item["rel_path"],
                                    "error": f"读取失败：{exc}"})
                    continue

                budget -= 1
                try:
                    judgement = review_duplicate(
                        protocol=cfg.ai.protocol,
                        base_url=cfg.ai.base_url, model=cfg.ai.model,
                        api_key=cfg.ai.api_key, timeout=cfg.ai.timeout_seconds,
                        audio={
                            "title": (meta.tags.get("TITLE") or [""])[0],
                            "artists": meta.tags.get("ARTIST") or [],
                            "album": (meta.tags.get("ALBUM") or [""])[0],
                            "duration": meta.info.duration,
                        },
                        existing_name=item["paired_sidecar"],
                        existing_text=existing_text,
                        orphan_name=item["rel_path"],
                        orphan_text=orphan_text,
                        use_proxy=cfg.ai.use_proxy,
                    )
                except AIError as exc:
                    results.append({"rel_path": item["rel_path"], "error": str(exc)})
                    continue

                # **能不能清理，由可测的重合度决定，不由模型一句话决定。**
                # 两份歌词正文重合 ≥95% 才算"内容重复"：少一行都是用户的东西，
                # 宁可留着让人看。模型只负责判断"是不是同一首歌/哪份更完整"。
                # 判据是「孤儿那份有没有多出一行歌词」：元信息（作词/作曲）
                # 不同不算内容不同。实际三份里两份是纯重复（多出 0 行歌词），
                # 一份多出 4 行真歌词——后者必须留下。
                enough_overlap = judgement.orphan_covered >= 0.999
                if not judgement.same_song or not enough_overlap:
                    action = "keep"
                elif judgement.keep == "orphan":
                    action = "replace"
                elif judgement.keep in ("existing", "either"):
                    action = "cleanup"
                else:
                    action = "keep"
                entry: dict[str, Any] = {
                    "rel_path": item["rel_path"],
                    "paired_sidecar": item["paired_sidecar"],
                    "audio": audio_rel,
                    "judgement": judgement.to_dict(),
                    "overlap": round(judgement.overlap, 3),
                    "action": action,
                    "applied": False,
                }
                if action == "keep" and judgement.same_song and not enough_overlap:
                    entry["note"] = ("这份歌词有自己的歌词行（现有那份没有），"
                                     "可能是不同版本，保留不动")
                confident = judgement.same_song and judgement.confidence >= min_conf
                if apply_now and confident and action in ("cleanup", "replace"):
                    try:
                        batch = time.strftime("%Y%m%d-%H%M%S")
                        orphan_path = base / item["rel_path"]
                        existing_path = base / item["paired_sidecar"]
                        if action == "cleanup":
                            _move_to_trash(cfg, root_cfg, orphan_path, batch)
                            entry["moved_to_trash"] = item["rel_path"]
                        else:
                            _move_to_trash(cfg, root_cfg, existing_path, batch)
                            os.rename(orphan_path, audio_path.with_suffix(".lrc"))
                            entry["replaced"] = item["paired_sidecar"]
                        _refresh_observation(root_cfg, audio_rel)
                        _invalidate_caches()
                        entry["applied"] = True
                    except OSError as exc:
                        entry["error"] = f"执行失败：{exc}"
                results.append(entry)
        return {"items": results, "applied": apply_now,
                "min_confidence": min_conf}

    @app.post("/api/v1/orphans/replace", dependencies=[Depends(require_auth)])
    @manual_lease()
    def replace_orphan_sidecar(payload: dict[str, Any]) -> dict[str, Any]:
        """用这份孤儿歌词**替换**音频当前配对的 `.lrc`。

        用在「同一首歌有两份歌词、内容不同」的场景（实际多来自
        「下载来的歌词」与「从内嵌歌词生成的 .lrc」并存）。
        旧文件不是删除，而是移进回收站（保留相对路径，可还原），
        然后再把这份孤儿改名成 `<音频同名>.lrc`。

        必须显式给 `confirm=true`：它会替换掉一首歌现有的歌词。
        """
        if not cfg.policy.write_enabled:
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        if not payload.get("confirm"):
            raise HTTPException(status_code=400, detail="请确认替换（confirm=true）")
        from .audit import audit_root

        root_name = str(payload.get("root") or "")
        rel = str(payload.get("path") or "")
        if not root_name or not rel:
            raise HTTPException(status_code=400, detail="必须提供 root 与 path")
        root_cfg, orphan = _safe_root_file(cfg, root_name, rel)
        if orphan.suffix.lower() != ".lrc":
            raise HTTPException(status_code=415, detail="只处理 .lrc 文件")

        target_rel = str(payload.get("target") or "").strip()
        base = root_cfg.resolved_path
        if target_rel:
            audio = _resolve_within_root(root_cfg, target_rel)
            if audio is None:
                raise HTTPException(status_code=404, detail="目标音频不存在")
        else:
            # 没指定就用审计给出的候选（同目录、唯一对应）
            from .audit import find_orphan_sidecars

            item = next(
                (o for o in find_orphan_sidecars(root_cfg)
                 if o["rel_path"] == rel), None
            )
            candidate = next(
                (c for c in (item or {}).get("candidates", [])
                 if c["scope"] == "same_dir"), None
            )
            if candidate is None:
                raise HTTPException(
                    status_code=404,
                    detail="找不到它对应的音频，请在界面上指定目标")
            audio = base / candidate["rel_path"]
        if _kind_of(audio) != "audio":
            raise HTTPException(status_code=415, detail="目标不是音频文件")

        # 音频当前配对到的 .lrc（同名或异名）：查审计，别猜路径
        audit = audit_root(root_cfg, only_rel_paths={str(audio.relative_to(base))})
        track = next(
            (t for t in audit.tracks
             if str(t.audio.rel_path) == str(audio.relative_to(base))), None
        )
        existing = track.sidecar.path if track is not None and track.sidecar.exists else None
        wanted = audio.with_suffix(".lrc")

        batch = time.strftime("%Y%m%d-%H%M%S")
        moved = None
        if existing is not None and existing != orphan and existing == wanted:
            moved = _move_to_trash(cfg, root_cfg, existing, batch)
        try:
            os.rename(orphan, wanted)
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"改名失败：{exc}") from exc
        _invalidate_caches()
        _refresh_observation(root_cfg, str(audio.relative_to(base)))
        _invalidate_caches()
        return {
            "ok": True,
            "orphan": rel,
            "audio": str(audio.relative_to(base)),
            "new_sidecar": str(wanted.relative_to(base)),
            "replaced": moved,
            "trash_batch": batch if moved else None,
        }

    @app.post("/api/v1/orphans/repair", dependencies=[Depends(require_auth)])
    @manual_lease()
    def repair_orphans(payload: dict[str, Any]) -> dict[str, Any]:
        """把名字不一致但确实是同一首的孤儿 `.lrc` 改成音频同名。

        `paths` 为空表示自动修所有**安全**的（同目录、目标无 .lrc、不覆盖）。
        `dry_run=true` 只返回计划，不动文件。
        """
        if not cfg.policy.write_enabled and not payload.get("dry_run"):
            raise HTTPException(
                status_code=409,
                detail="配置里写入开关已关闭",
            )
        from .pipeline import repair_sidecar_names

        root_name = payload.get("root") or (cfg.roots[0].name if cfg.roots else None)
        roots = [r for r in cfg.enabled_roots if not root_name or r.name == root_name]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")
        raw_paths = payload.get("paths") or []
        paths = {str(p) for p in raw_paths if str(p).strip()} or None
        dry_run = bool(payload.get("dry_run"))

        out = []
        for root in roots:
            named = repair_sidecar_names(
                state, cfg, root, only_paths=paths, dry_run=dry_run)
            out.append({"root": root.name, **named})
        return {"roots": out}

    @app.get("/api/v1/orphans", dependencies=[Depends(require_auth)])
    def orphans(root: str | None = None) -> dict[str, Any]:
        """无主的 .lrc 列表：它最可能属于哪首音频、那首音频是否已有歌词。

        「多余歌词」这件事必须区分两种情况，否则修复动作会截然相反：

        - **音频已经有歌词** → 这一份是多余的，正确处理是清理（移入回收站）。
          实际曲库里两条孤儿都属于这一类，而且内容与已配对的那份
          **逐字节相同**。若按「重新配对」改名，反而会与既有 lrc 同名冲突。
        - **音频没有歌词** → 改名配对才是修复。

        判据由后端给出（`audit.find_orphan_sidecars`），前端只负责展示，
        不各自猜一遍。
        """
        from .audit import find_orphan_sidecars

        roots = [r for r in cfg.enabled_roots if not root or r.name == root]
        if not roots:
            raise HTTPException(status_code=400, detail="没有匹配的受管根")

        def compute() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for root_cfg in roots:
                for entry in find_orphan_sidecars(root_cfg):
                    entry["root"] = root_cfg.name
                    items.append(entry)
            return items

        items = _CACHE.get_or_compute(
            "orphans:" + ",".join(r.name for r in roots), compute
        )
        return {
            "total": len(items),
            "items": items,
            "duplicates": sum(1 for i in items if i["action"] == "duplicate"),
            "same_content": sum(1 for i in items if i["same_content"]),
        }

    @app.get("/api/v1/files/content", dependencies=[Depends(require_auth)])
    def file_content(root: str, path: str) -> dict[str, Any]:
        """文本类文件的预览。`.lrc` 会额外返回逐行解析结果，便于对齐显示。"""
        root_cfg, resolved = _safe_root_file(cfg, root, path)
        kind = _kind_of(resolved)
        if kind not in ("lyrics", "text"):
            raise HTTPException(status_code=415, detail=f"该类型不支持文本预览: {kind}")

        raw = resolved.read_bytes()
        truncated = len(raw) > _MAX_TEXT_PREVIEW_BYTES
        if truncated:
            raw = raw[:_MAX_TEXT_PREVIEW_BYTES]
        text, has_bom = decode_lyrics_bytes(raw)

        payload: dict[str, Any] = {
            "root": root_cfg.name,
            "rel_path": path,
            "kind": kind,
            "size": resolved.stat().st_size,
            "truncated": truncated,
            "has_bom": has_bom,
        }
        if kind == "lyrics":
            parsed = parse_lrc(text)
            # 歌词编辑器需要原文（行视图会丢元信息行与原始时间戳写法）
            payload["text"] = text
            payload.update(
                {
                    "state": parsed.state.value,
                    "line_count": parsed.line_count,
                    "timestamp_count": parsed.timestamp_count,
                    "offset_ms": parsed.offset_ms,
                    "has_word_level": parsed.has_word_level,
                    "tags": parsed.tags,
                    "problems": list(parsed.problems),
                    "lines": [
                        {"ms": ln.ms, "stamp": ln.stamp, "text": ln.text}
                        for ln in parse_lrc_lines(text)
                    ],
                }
            )
        else:
            payload["text"] = text
        return payload

    @app.get("/api/v1/files/media", dependencies=[Depends(require_auth)])
    def file_media(root: str, path: str) -> FileResponse:
        """音频与图片的原字节。Starlette 的 FileResponse 原生支持 Range 请求，
        因此音频可以拖动进度条试听。"""
        _root_cfg, resolved = _safe_root_file(cfg, root, path)
        kind = _kind_of(resolved)
        if kind not in ("audio", "image"):
            raise HTTPException(status_code=415, detail=f"该类型不支持二进制预览: {kind}")
        return FileResponse(resolved, filename=resolved.name)

    @app.get("/api/v1/files/meta", dependencies=[Depends(require_auth)])
    def file_meta(root: str, path: str) -> dict[str, Any]:
        """音频的标签与歌词状态，用于预览面板。"""
        _root_cfg, resolved = _safe_root_file(cfg, root, path)
        if _kind_of(resolved) != "audio":
            raise HTTPException(status_code=415, detail="只有音频支持标签预览")
        base = _root_cfg.resolved_path
        meta = read_audio_meta(resolved, base)
        # 标签编辑器需要**全部**字段，而 meta.tags 只是匹配用的白名单子集。
        # 两者都给：tags 供匹配相关展示，all_tags 供编辑。
        all_tags, tags_error = read_all_tags(resolved)
        # 内嵌歌词原文：没有 .lrc 的曲目也能在详情里预览 / 编辑内嵌歌词
        embedded_text = ""
        embedded_text_error = None
        if meta.embedded.state.value != "absent":
            try:
                texts, err = read_embedded_lyrics_texts(resolved)
                embedded_text = "\n".join(texts)
                embedded_text_error = err
            except Exception as exc:  # noqa: BLE001 — 读取失败不阻塞标签预览
                embedded_text_error = str(exc)
        return {
            "rel_path": path,
            "all_tags": all_tags,
            "all_tags_error": tags_error,
            "media_format": meta.info.media_format.value,
            "extension": meta.info.extension,
            "extension_matches_container": meta.info.extension_matches_container,
            "duration": meta.info.duration,
            "size": meta.info.size,
            "parse_error": meta.info.parse_error,
            "tags": meta.tags,
            "embedded_state": meta.embedded.state.value,
            "embedded_fields": list(meta.embedded.fields),
            "has_cover": meta.embedded.has_cover,
            "embedded_text": embedded_text,
            "embedded_text_error": embedded_text_error,
            "problems": list(meta.embedded.parsed.problems) if meta.embedded.parsed else [],
        }

    # -- 前端 -------------------------------------------------------------

    #: 构建产物：`web/index.html` + `web/assets/<name>-<hash>.{js,css}`。
    #: 文件名带内容哈希，所以可以长期缓存；页面本身必须每次确认新鲜度。
    assets_dir = web_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", _ImmutableStatic(directory=str(assets_dir)), name="assets")

    #: 页面必须每次向服务器确认新鲜度。
    #: 实际反复出现的困扰：部署了新版本，浏览器却继续用缓存的旧页面——
    #: 于是「明明改了却看不见」，很难分清是没改对还是没刷新。
    #: 这是一个权限受控的内部工具，页面也就一百多 KB，
    #: 每次都重新取取代价极小，远比让人怀疑改动是否生效划算。
    _NO_CACHE = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    def _page() -> HTMLResponse:
        page = web_dir / "index.html"
        if not page.is_file():
            return HTMLResponse(
                "<h1>Lyra</h1><p>前端资源缺失。API 见 <a href='/api/docs'>/api/docs</a>。</p>",
                status_code=200,
                headers=_NO_CACHE,
            )
        return HTMLResponse(page.read_text(encoding="utf-8"), headers=_NO_CACHE)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return _page()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> HTMLResponse:
        """前端路由（/library、/review…）刷新时必须回到同一个页面。

        没有这条回退，在子页面按 F5 会得到 404——而 SPA 的地址栏
        是会被收藏、会被分享的，这属于「部署完才发现」的典型问题。
        `/api/` 前缀**不能**回退：那会让打错的接口返回 HTML，
        脚本收到 200 再去 `json.loads` 就变成了另一种更难懂的错。
        """
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        return _page()

    return app


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 文件浏览：类型判定、路径安全、缓存
# ---------------------------------------------------------------------------

#: 文本预览上限。超过就截断——不能因为一个畸形大文件把内存吃光，
#: 也不该让 API 变成任意大小的文件下载器（音频走 /files/media）。
_MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024

#: 上传替换的大小上限。无损音频单文件可到几百 MB，给 1GB 余量。
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024

#: 封面大小上限。内嵌封面每个文件都存一份，太大浪费空间。
_MAX_COVER_BYTES = 20 * 1024 * 1024

#: 扩展名 → 预览类型。**只允许这些类型被读取**，
#: 其它文件（数据库、脚本、配置…）既不在列表里显示内容、也无法被读取。
_KIND_BY_EXT: dict[str, str] = {}
for _ext in (".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".aiff", ".ape", ".wv"):
    _KIND_BY_EXT[_ext] = "audio"
for _ext in (".lrc",):
    _KIND_BY_EXT[_ext] = "lyrics"
for _ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
    _KIND_BY_EXT[_ext] = "image"
for _ext in (".nfo", ".m3u", ".m3u8", ".cue", ".txt", ".log", ".json", ".md"):
    _KIND_BY_EXT[_ext] = "text"

_PREVIEWABLE_KINDS = frozenset({"audio", "lyrics", "image", "text"})


_TAG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _validated_tag_key(key: object) -> str:
    """校验并规范化标签字段名。

    为什么必须严格校验：字段名会**直接写进音频文件的元数据块**。
    含 `=`、换行或空字节的名字会破坏 Vorbis comment 的结构，
    轻则标签错乱，重则整个文件的元数据无法解析。
    """
    name = str(key).strip().upper()
    if not _TAG_KEY_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"非法标签名 {key!r}：只允许字母开头、字母数字下划线，最长 64 字符",
        )
    return name


@contextlib.contextmanager
def _manual_lease(
    state: StateStore, *, wait_seconds: float = 30.0, label: str = "manual"
) -> Iterator[dict[str, Any]]:
    """人工操作取写租约：**等**，而不是直接失败。

    规则很明确——人工优先于自动。监控进程每写完一个文件都会检查
    `write_waits`，看到有人在等就主动收手；所以这里的等待通常只有一两秒。

    仍然拿不到（例如监控卡在一次网络请求里）就如实报错，
    而不是静默把人工操作降级成「已选用、稍后写入」。
    """
    owner = f"{label}-{uuid.uuid4().hex[:8]}"
    state.request_write(owner)
    acquired = False
    try:
        acquired = state.acquire_write_lock(
            "write", owner, ttl_seconds=900, wait_seconds=wait_seconds,
        )
        yield {
            "acquired": acquired, "owner": owner,
            "holder": None if acquired else state.write_lock_holder("write"),
        }
    finally:
        state.cancel_write_request(owner)
        if acquired:
            state.release_write_lock("write", owner)


def _move_to_trash(cfg: Config, root: RootConfig, path: Path, batch: str) -> str:
    """兼容旧调用点：真正的实现在 `fileops.move_to_trash`。"""
    return move_to_trash(cfg, root, path, batch)


def _kind_of(path: Path) -> str:
    return _KIND_BY_EXT.get(path.suffix.lower(), "other")


#: 同目录封面文件的候选名，按「音乐库里最常用的顺序」排。
#: 大小写不敏感——`Cover.JPG`、`COVER.jpg` 在本地网络都很常见。
_SIBLING_COVER_NAMES: tuple[str, ...] = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.png", "albumart.jpg", "albumartsmall.jpg",
)

#: 兜底：以这些前缀开头的任意图片（`cover-1.jpg`、`folder_back.png` 之类）
_SIBLING_COVER_PREFIXES: tuple[str, ...] = ("cover", "folder", "front", "album")


def _find_sibling_cover(audio_path: Path) -> Path | None:
    """在音频所在目录里找一张封面图。

    **必须真的存在才算**：库里不少专辑（尤其单曲、EP）根本没有 cover 文件，
    界面上就不该出现一个点了什么都不会发生的按钮。
    找不到返回 None，由调用方决定「隐藏按钮」还是「报错」。
    """
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
        if hit is not None and _kind_of(hit) == "image":
            return hit
    for key in sorted(entries):
        if key.startswith(_SIBLING_COVER_PREFIXES) and _kind_of(entries[key]) == "image":
            return entries[key]
    return None


def _select_root(cfg: Config, name: str | None) -> RootConfig:
    if not cfg.roots:
        raise HTTPException(status_code=400, detail="没有配置受管根")
    if name is None:
        return cfg.roots[0]
    found = next((r for r in cfg.roots if r.name == name), None)
    if found is None:
        raise HTTPException(status_code=400, detail=f"未知的受管根: {name}")
    return found


#: 「文件里已经有歌词」的观测状态。空 sidecar、无法解析的 sidecar 都不算——
#: 那些是**待修的问题**，不是要保护的成果。
_LYRICS_BEARING_STATES = frozenset({"synced", "plain_text", "synced_suspicious"})


def _has_existing_lyrics(state: StateStore, location_id: int) -> bool:
    """该位置最近一次观测里是否已经有歌词（内嵌或 .lrc）。

    用观测而不是现读文件：观测就是审计/监控对「此刻文件里有什么」的记录，
    而这里要做的是**保护已有成果**。宁可多问一次，也不能静默覆盖。
    """
    row = state.conn.execute(
        "SELECT embedded_state, sidecar_state FROM lyric_observations "
        "WHERE location_id=? ORDER BY observed_at DESC LIMIT 1",
        (location_id,),
    ).fetchone()
    if row is None:
        return False
    return (
        str(row["embedded_state"] or "") in _LYRICS_BEARING_STATES
        or str(row["sidecar_state"] or "") in _LYRICS_BEARING_STATES
    )


def _lyrics_already_same(state: StateStore, location_id: int, digest: str) -> bool:
    """该位置最近一次观测里是否已经是**这一份**歌词（内容指纹相同）。

    与 `_has_existing_lyrics` 的区别：那个只问「有没有歌词」，
    这个问「是不是同一份」。写完要如实回执——「文件里本来就是这一份」
    与「已经写好了」是两回事，混成一句会被当成写入失败。
    """
    if not digest:
        return False
    row = state.conn.execute(
        "SELECT embedded_hash, sidecar_hash FROM lyric_observations "
        "WHERE location_id=? ORDER BY observed_at DESC LIMIT 1",
        (location_id,),
    ).fetchone()
    if row is None:
        return False
    return digest in (
        str(row["embedded_hash"] or ""),
        str(row["sidecar_hash"] or ""),
    )


def _locate_audio(
    cfg: Config, root_name: Any, rel_path: Any
) -> tuple[RootConfig, Path, str]:
    """把 (受管根, 相对路径) 解析成根内的音频文件，并确认它是可解析的音频。

    单曲手工选歌词的三个接口都要先做这件事。安全约束与删除/替换完全一致：
    拒绝绝对路径、`..`、符号链接，resolve 后必须仍在根内。
    """
    rel = str(rel_path or "").strip().replace("\\", "/")
    if not rel:
        raise HTTPException(status_code=400, detail="必须提供 path")
    root = _select_root(cfg, str(root_name) if root_name else None)
    target = _resolve_within_root(root, rel)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或不在受管根内")
    meta = read_audio_meta(target, root.resolved_path)
    if meta.info.parse_error is not None or not meta.info.media_format.supported:
        raise HTTPException(
            status_code=409,
            detail=f"这个文件不是可解析的音频（{meta.info.media_format.value}），无法刮削歌词",
        )
    return root, target, rel


#: 短链跳转用的 UA。QQ 音乐的分享短链对默认的 httpx UA 会返回一个空页面，
#: 用一个普通手机浏览器的 UA 才稳定跳到正式播放页（实际 iPhone UA 可用）。
_SHORT_LINK_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _resolve_short_link(url: str) -> str:
    """跟随短链跳转，返回最终地址。

    QQ 音乐 App 分享出来的 `c6.y.qq.com/base/fcgi-bin/u?__=xxx` 与网易云的
    `163cn.tv/xxx` 都要经过 302 才能看到歌曲 id，这一步只能联网做。

    短链都是国内的，**不走代理**：`client_for` 对未知功能一律直连。
    """
    try:
        with client_for(
            "shortlink",
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": _SHORT_LINK_UA},
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"打不开这个短链接：{type(exc).__name__}: {exc}"
        ) from exc
    return str(response.url)


def _fetch_candidate_lyrics(
    cfg: Config,
    root: RootConfig,
    rel: str,
    provider_name: str,
    provider_id: str,
    *,
    allow_direct: bool = False,
):
    """按 (来源, 候选 id) 现取一次歌词。

    **正文一律由服务端重新向来源取回**，接口不接受客户端提交歌词内容——
    否则这个接口就成了「往音乐库里写任意内容」的入口，而这恰恰是本工具
    最不该有的东西。与 `/staged/{id}/choose` 的口径一致。

    默认先搜索、再在结果里按 id 找候选。这有个反直觉的后果：**使用者手工
    指定的那首歌，很可能根本不在搜索结果里**——同名歌曲几十条时，
    搜索排序里甚至没有它。所以 `allow_direct=True`（人工链接的来源）时，
    搜索里找不到就按 id 直接取词。不默认开启是为了保持诚实：对「系统自己
    找出来的候选」仍然要求它能在搜索结果里复现。
    """
    from .providers import create_provider

    try:
        provider = create_provider(provider_name, cfg.providers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = root.resolved_path
    meta = read_audio_meta(base / rel, base)
    context = build_context(meta.tags, meta.info.duration)
    query = TrackQuery(
        title=context.title,
        artists=context.artists,
        album=context.album,
        duration=context.duration,
        musicbrainz_trackid=context.musicbrainz_trackid,
        isrc=context.isrc,
    )
    try:
        candidate = Candidate(
            provider=provider_name, provider_id=provider_id, title="", artists=()
        )
        if not allow_direct:
            candidates = provider.search(query)
            found = next((c for c in candidates if c.provider_id == provider_id), None)
            if found is None:
                raise HTTPException(
                    status_code=404,
                    detail="该来源已找不到这个候选（可能已下架），请重新查询候选列表",
                )
            candidate = found
        raw = provider.lyrics(candidate)
    except HTTPException:
        raise
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"来源请求失败：{exc}") from exc
    finally:
        provider.close()

    if raw is None or not raw.text.strip():
        raise HTTPException(status_code=409, detail="该来源这首歌没有可用歌词")
    return raw


def _resolve_within_root(root: RootConfig, rel: str) -> Path | None:
    """把「受管根 + 相对路径」解析成根内的真实路径，**不限制文件类型**。

    与 `_safe_root_file` 的分工：后者用于预览，只放行白名单类型；
    这里用于删除/替换，需要能处理任意文件（音乐、歌词、图片、以及用户
    自己放进去的杂物）。安全约束完全一致：拒绝绝对路径、`..`、符号链接，
    resolve 后必须仍在根内。
    """
    base = root.resolved_path.resolve()
    if not rel or "\x00" in rel:
        return None
    if Path(rel).is_absolute() or rel.startswith(("/", "\\")):
        return None
    if any(part in ("..", "") for part in Path(rel).parts):
        return None
    candidate = base.joinpath(*Path(rel).parts)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(base):
        return None
    return candidate


def _safe_root_file(cfg: Config, root_name: str, rel: str) -> tuple[RootConfig, Path]:
    """把「受管根 + 相对路径」解析成一个**确认在根内**的真实文件。

    这是预览功能的安全边界，逐条都必要：

    1. 拒绝绝对路径与空路径
    2. 逐段拒绝 `..`（不能靠字符串替换，必须按路径分量判断）
    3. 拒绝符号链接（否则根内的链接可以指向根外）
    4. `resolve(strict=True)` 后必须仍在根内——软链、`..`、挂载绕道都在这里被挡住
    5. 必须是普通文件，且类型在可预览白名单内

    任何一条不满足都直接拒绝，不做「尽量放行」。
    """
    root = _select_root(cfg, root_name)
    base = root.resolved_path.resolve()

    if not rel or "\x00" in rel:
        raise HTTPException(status_code=400, detail="非法路径")
    if Path(rel).is_absolute() or rel.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="不接受绝对路径")
    if any(part in ("..", "") for part in Path(rel).parts):
        raise HTTPException(status_code=400, detail="路径不得包含 .. ")

    candidate = base.joinpath(*Path(rel).parts)
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="不跟随符号链接")

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"文件不存在: {exc}") from exc

    if not resolved.is_relative_to(base):
        raise HTTPException(status_code=400, detail="路径越界")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="不是普通文件")
    if _kind_of(resolved) not in _PREVIEWABLE_KINDS:
        raise HTTPException(status_code=415, detail=f"不支持的预览类型: {resolved.suffix}")
    return root, resolved


def _root_for_location(cfg: Config, state: StateStore, root_id: Any) -> RootConfig:
    """把数据库里的 `root_id` 映射回配置中的受管根。

    不能图省事直接用 `cfg.roots[0]`：配置可能声明多个根，认错根会让预览与
    对比跑到完全不同的目录去。先查数据库里记录的路径，再按**解析后的绝对
    路径**匹配配置项；实在匹配不上才退回第一个，保证不抛异常。
    """
    if not cfg.roots:
        raise HTTPException(status_code=400, detail="没有配置受管根")
    if root_id is not None:
        try:
            row = state.conn.execute(
                "SELECT path FROM roots WHERE id=?", (int(root_id),)
            ).fetchone()
        except (TypeError, ValueError):
            row = None
        if row is not None and row["path"]:
            target = str(Path(str(row["path"])).resolve())
            for root in cfg.roots:
                if str(root.resolved_path) == target:
                    return root
    return cfg.roots[0]


class _TtlCache:
    """极简 TTL 缓存。

    统计与文件清单都要遍历整个曲库；页面每次刷新都重扫既慢又浪费 I/O。
    缓存很短（默认 30 秒），足以挡住连续刷新，又不会让页面显示过期数据。
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: str, compute, *, refresh: bool = False):  # noqa: ANN001, ANN202
        now = time.time()
        with self._lock:
            if not refresh:
                hit = self._items.get(key)
                if hit is not None and now - hit[0] < self._ttl:
                    return hit[1]
        value = compute()
        with self._lock:
            self._items[key] = (now, value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._items.clear()


_CACHE = _TtlCache(ttl_seconds=30.0)

#: 统计单独缓存，TTL 更长。
#:
#: 计算一次统计要遍历整个曲库（本机 1810 首约 3.4 秒，NAS 上也是秒级），
#: 30 秒就过期意味着**每半分钟就可能有人等到一次全库扫描**——页面看起来
#: 像是「有时快有时卡」。而覆盖率这类数字半分钟内不会有意义地变化。
#: 「重新统计」按钮走 refresh=True，需要立刻刷新时仍然可以强制。
_STATS_CACHE = _TtlCache(ttl_seconds=600.0)


def _invalidate_caches() -> None:
    """写操作后让文件清单缓存失效。

    统计缓存**不在这里清**：自动化（标签扫描 / AI 预审 / 封面嵌入）每次写入
    都会调用本函数。如果清掉统计缓存，用户每次进首页都要冷启动重算全库
    （4500 首 ~30 秒），看到的永远是「正在统计曲库」。

    改用后台预热线程每 3 分钟刷新一次——数据最多滞后 3 分钟，
    但页面加载始终命中缓存、瞬间呈现。
    """
    _CACHE.invalidate()


def _stats(cfg: Config, *, refresh: bool = False) -> dict[str, Any]:
    def compute() -> dict[str, Any]:
        return {
            "roots": [collect_stats(r).to_dict() for r in cfg.enabled_roots],
            "write_enabled": cfg.policy.write_enabled,
            # 启用的来源。诊断面板要能回答「到底接了哪几个源」，
            # 而原来它读 `s.providers`——这个键根本不存在，永远显示「—」。
            "providers": [{"name": name} for name in cfg.providers.enabled],
        }

    return _STATS_CACHE.get_or_compute("stats", compute, refresh=refresh)


def _filter_by_lyrics(
    store: StateStore,
    root_id: int,
    *,
    lyric_state: str,
    missing_sidecar: bool,
    missing_embedded: bool,
) -> set[str]:
    """按「歌词现状」筛出曲目，供刮削任务限定范围。

    条件是**与**关系，可以组合：

    - `lyric_state = "none"`：内嵌与外挂都没有歌词；
    - `lyric_state = "unsynced"`：有歌词，但没有任何一侧带时间轴
      （纯文本、占位、时间轴异常都算「没有可用的时间轴」）；
    - `missing_sidecar`：没有 .lrc 文件（空的也算没有）；
    - `missing_embedded`：没有内嵌歌词。

    两组条件之间是**与**；两个缺失项之间是**或**——同时勾选表示
    「缺 .lrc 或 缺内嵌歌词」，因为勾两项的人通常想补齐任意一侧，
    而不是只看「两样都缺」的那一小撮（那等价于「没有任何歌词」，
    已经可以用状态筛选表达）。

    判据取每个位置**最近一次观测**——它正是「此刻文件里有什么」。
    """
    rows = store.conn.execute(
        """SELECT loc.rel_path, obs.embedded_state, obs.sidecar_state
             FROM locations loc
             JOIN lyric_observations obs
               ON obs.id = (SELECT MAX(id) FROM lyric_observations
                             WHERE location_id = loc.id)
            WHERE loc.root_id = ? AND loc.missing_since IS NULL""",
        (root_id,),
    ).fetchall()

    def present(state: str | None) -> bool:
        return state is not None and state != "absent"

    out: set[str] = set()
    for row in rows:
        embedded = row["embedded_state"]
        sidecar = row["sidecar_state"]
        has_embedded = present(embedded)
        has_sidecar = present(sidecar)
        synced = embedded == "synced" or sidecar == "synced"

        if lyric_state == "none" and (has_embedded or has_sidecar):
            continue
        if lyric_state == "unsynced" and (not (has_embedded or has_sidecar) or synced):
            continue
        missing_any = False
        if missing_sidecar and sidecar is None:
            missing_any = True
        if missing_embedded and embedded in (None, "absent"):
            missing_any = True
        if (missing_sidecar or missing_embedded) and not missing_any:
            continue
        out.add(str(row["rel_path"]))
    return out


def _list_files(
    cfg: Config,
    *,
    root: str | None,
    kind: str | None,
    q: str | None,
    page: int,
    size: int,
) -> dict[str, Any]:
    def compute() -> list[dict[str, Any]]:
        from . import scan

        items: list[dict[str, Any]] = []
        for root_cfg in cfg.enabled_roots:
            base = root_cfg.resolved_path
            if not base.is_dir():
                continue
            for path in scan._walk(base, root_cfg):  # noqa: SLF001 — 复用同一套忽略规则
                file_kind = _kind_of(path)
                if file_kind not in _PREVIEWABLE_KINDS:
                    continue
                try:
                    stat = path.stat()
                    rel = str(path.relative_to(base))
                except OSError:
                    continue
                items.append(
                    {
                        "root": root_cfg.name,
                        "rel_path": rel,
                        "name": path.name,
                        "kind": file_kind,
                        "ext": path.suffix.lower(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
        items.sort(key=lambda i: (i["root"], i["rel_path"]))
        return items

    all_items = _CACHE.get_or_compute("files", compute)

    scoped = all_items
    if root:
        scoped = [i for i in scoped if i["root"] == root]

    # 类型计数按「整个受管范围」统计，不随搜索词或当前类型变化：
    # 下拉里的数字如果跟着输入跳，会被误读成「库里只剩这么多了」。
    counts: dict[str, int] = {}
    for item in scoped:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    selected = scoped
    if kind:
        selected = [i for i in selected if i["kind"] == kind]
    if q:
        needle = q.lower()
        selected = [i for i in selected if needle in i["rel_path"].lower()]

    total = len(selected)
    start = max(0, (page - 1) * size)

    return {
        "total": total,
        "page": page,
        "size": size,
        "by_kind": counts,
        "items": selected[start : start + size],
    }


# ---------------------------------------------------------------------------
# 查询实现
# ---------------------------------------------------------------------------


def _summary(cfg: Config, store: StateStore, runner: Runner) -> dict[str, Any]:
    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = store.conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    lyrics_states = {
        row["state"]: row["n"]
        for row in store.conn.execute(
            "SELECT state, COUNT(*) AS n FROM staged_lyrics GROUP BY state"
        )
    }
    outcomes = {
        row["outcome"]: row["n"]
        for row in store.conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM resolutions GROUP BY outcome"
        )
    }
    issue_totals: dict[str, int] = {}
    # **只统计还存在的文件。** 漏掉这个条件时，一条「已标记缺失」的旧位置
    # 照样会把它的缺陷计进总数：筛选下拉显示「缺 MusicBrainz ID (1)」，
    # 点进去却一条也没有——使用者会以为筛选坏了（实际就是这么发现的）。
    for row in store.conn.execute(
        """SELECT o.issues FROM lyric_observations o
             JOIN locations loc ON loc.id = o.location_id
            WHERE loc.missing_since IS NULL
              AND o.id IN (SELECT MAX(id) FROM lyric_observations GROUP BY location_id)"""
    ):
        try:
            for code in json.loads(row["issues"] or "[]"):
                issue_totals[code] = issue_totals.get(code, 0) + 1
        except (TypeError, ValueError):
            continue

    # 歌词的**来源**分布：哪个来源真正在供词。实际结论是差异极大
    # （QQ 命中最多，LRCLIB 几乎不出结果），图上比一句「已接入三个来源」有用得多。
    by_provider: dict[str, dict[str, int]] = {}
    for row in store.conn.execute(
        "SELECT provider, state, COUNT(*) AS n FROM staged_lyrics "
        "GROUP BY provider, state"
    ):
        by_provider.setdefault(str(row["provider"]), {})[str(row["state"])] = int(row["n"])

    # 处理进度时间线：按天累计「已写入」与「进入复核」。
    #
    # 用**累计**而不是每日增量：进度条的意义是「离目标还有多远」，
    # 每日增量在图上是一堆锯齿，看不出趋势。
    # 只取最近 60 天，避免随着时间推移把整表读进来。
    cutoff = time.time() - 60 * 86400
    daily: dict[str, dict[str, int]] = {}
    for row in store.conn.execute(
        "SELECT created_at, state FROM staged_lyrics WHERE created_at >= ?",
        (cutoff,),
    ):
        day = time.strftime("%Y-%m-%d", time.localtime(float(row["created_at"])))
        slot = daily.setdefault(day, {"applied": 0, "review": 0})
        if str(row["state"]) in ("applied", "already_satisfied"):
            slot["applied"] += 1
        elif str(row["state"]) == "needs_review":
            slot["review"] += 1
    history: list[dict[str, Any]] = []
    written = 0
    reviewed = 0
    for day in sorted(daily):
        written += daily[day]["applied"]
        reviewed += daily[day]["review"]
        history.append(
            {"date": day, "written": written, "reviewed": reviewed,
             "written_today": daily[day]["applied"]}
        )

    return {
        "version": __version__,
        "schema_version": store.current_schema_version(),
        "integrity": store.integrity_check(),
        "write_enabled": cfg.policy.write_enabled,
        "lyrics_by_provider": by_provider,
        "coverage_history": history,
        "roots": [
            {"name": r["name"], "role": r["role"], "path": r["path"], "health": r["health"]}
            for r in store.conn.execute("SELECT * FROM roots ORDER BY name")
        ],
        "counts": {
            "locations": scalar(
                "SELECT COUNT(*) FROM locations WHERE missing_since IS NULL"
            ),
            "observations": scalar("SELECT COUNT(*) FROM lyric_observations"),
            "staged_pending": scalar(
                "SELECT COUNT(*) FROM staged_lyrics WHERE state='staged'"
            ),
            "needs_review": scalar(
                "SELECT COUNT(*) FROM staged_lyrics WHERE state='needs_review'"
            ),
            # 按曲目去重后的待审查数量。界面按曲目分页，侧栏徽标与页头
            # 显示同一个口径，否则会出现「徽标 96、页面 82」这种自相矛盾。
            "review_tracks": scalar(
                "SELECT COUNT(DISTINCT location_id) FROM staged_lyrics "
                "WHERE state='needs_review'"
            ),
            "applied": scalar(
                "SELECT COUNT(*) FROM staged_lyrics WHERE state='applied'"
            ),
            "unfinished_transactions": len(store.unfinished_transactions()),
            "active_renames": sum(1 for _ in store.iter_renames()),
        },
        "staged_by_state": lyrics_states,
        "resolutions_by_outcome": outcomes,
        "issues": dict(sorted(issue_totals.items(), key=lambda kv: -kv[1])),
        "busy": runner.busy(),
    }


def _tracks(
    store: StateStore,
    *,
    state_filter: str | None,
    q: str | None,
    page: int,
    size: int,
    issue: str | None = None,
) -> dict[str, Any]:
    # 排除「本次核对没见到」的位置：目录被改过之后，库里会留下指向
    # 已不存在路径的记录，它们不该继续算作曲目（实际 423 条里 219 条如此）。
    where: list[str] = ["loc.missing_since IS NULL"]
    params: list[Any] = []
    if issue:
        # issues 在库里是 JSON 数组文本。用 LIKE 匹配 `"code"`（带引号）
        # 以避免子串误命中——例如 missing_isrc 与 missing_isrc_x。
        where.append("obs.issues LIKE ?")
        params.append(f'%"{issue}"%')
    if state_filter:
        where.append(
            "(obs.embedded_state = ? OR obs.sidecar_state = ?)"
        )
        params.extend([state_filter, state_filter])
    if q:
        where.append("loc.rel_path LIKE ?")
        params.append(f"%{q}%")

    base_sql = f"""
        FROM locations loc
        JOIN roots ro ON ro.id = loc.root_id
        LEFT JOIN lyric_observations obs
               ON obs.id = (SELECT MAX(id) FROM lyric_observations WHERE location_id = loc.id)
        WHERE {' AND '.join(where)}
    """
    total = store.conn.execute(f"SELECT COUNT(*) {base_sql}", tuple(params)).fetchone()[0]
    rows = store.conn.execute(
        f"""SELECT loc.id, loc.rel_path, loc.size, loc.mtime, ro.name AS root_name,
                   obs.embedded_state, obs.sidecar_state, obs.issues, obs.sidecar_size
            {base_sql}
            ORDER BY loc.rel_path
            LIMIT ? OFFSET ?""",
        (*params, size, (page - 1) * size),
    ).fetchall()

    return {
        "total": int(total),
        "page": page,
        "size": size,
        "items": [
            {
                "id": r["id"],
                "rel_path": r["rel_path"],
                "root": r["root_name"],
                "size": r["size"],
                "embedded_state": r["embedded_state"],
                "sidecar_state": r["sidecar_state"],
                "sidecar_size": r["sidecar_size"],
                "issues": json.loads(r["issues"] or "[]"),
            }
            for r in rows
        ],
    }




def _strip_markdown(text: object) -> str | None:
    """去掉文案里的 Markdown 行内标记。

    理由文案曾写成 `时长相同**不能**区分版本`，而界面是纯文本渲染，
    **会原样显示成星号**。源码已修正，但**库里已有的 candidate_json 快照
    是修复前写进去的**——不清掉的话，使用者看到的仍是星号。

    这些快照会在曲目被重新刮削时自然更新，但在此之前读取层必须兜住：
    界面正确比「等它自己好」重要。
    """
    if not isinstance(text, str):
        return None
    cleaned = text.replace("**", "").replace("`", "")
    return cleaned or None



#: 闸门标签的旧写法 → 新写法。
#:
#: 旧标签写的是**期望结果**（「标题相似度达标」），于是未通过时界面读成
#: 「标题相似度达标：未通过」——自相矛盾。新标签只描述**检查项本身**，
#: 通过与否由状态标记承载。
#:
#: 源码已改，但库里已有的 candidate_json 快照仍是旧标签，
#: 所以读取层要归一——否则界面继续显示矛盾文案。
_GATE_LABEL_FIXES = {
    "时长在硬闸门内": "时长差",
    "版本证据一致": "版本证据",
    "标题相似度达标": "标题相似度",
    "艺人相似度达标": "艺人相似度",
    "总分达到自动接受门槛": "匹配总分",
}


def _normalise_gate_label(label: object) -> str | None:
    if not isinstance(label, str):
        return None
    return _GATE_LABEL_FIXES.get(label, label)


def _staged_reason(candidate_json: object) -> dict[str, Any]:
    """从 resolutions 的候选快照里取出判定依据。

    只挑出队列需要展示的部分：一句话结论、完整建议、以及**未通过**的闸门。
    通过的闸门不进列表——队列空间有限，没过的才需要看。

    解析失败时返回空结构而不是抛错：一条旧记录格式不对，
    不该让整个复核队列打不开。
    """
    if not candidate_json:
        return {
            "reason": None, "recommendation": None, "failed_checks": [],
            "has_evidence": False,
        }
    try:
        payload = json.loads(candidate_json) if isinstance(candidate_json, str) else candidate_json
        if not isinstance(payload, dict):
            raise TypeError("候选快照不是对象")
    except (TypeError, ValueError):
        return {
            "reason": None, "recommendation": None, "failed_checks": [],
            "has_evidence": False,
        }

    recommendation = payload.get("recommendation")
    checks = payload.get("checks") or []
    failed = [
        c for c in checks
        if isinstance(c, dict) and not c.get("passed")
    ]
    reason = None
    if isinstance(recommendation, dict):
        reason = recommendation.get("text")
    if not reason and failed:
        # 没有建议文案时，用第一道没过的闸门兜底——总比只显示分数强
        first = failed[0]
        reason = f"{first.get('label', '判定')}未通过：{first.get('detail', '')}"
    return {
        "has_evidence": True,
        "reason": _strip_markdown(reason),
        "recommendation": recommendation,
        "failed_checks": [
            {
                "key": c.get("key"),
                "label": _normalise_gate_label(c.get("label")),
                "detail": c.get("detail"),
            }
            for c in failed
        ],
    }


def _safe_batch_entries(store: StateStore) -> list[dict[str, Any]]:
    """「推荐采纳」的判定：硬闸门全过、仅软门槛（总分）未达。

    与 `/staged/safe-batch`、以及列表接口的 `exclude_safe` 共用同一份实现。
    两处各写一套的话，迟早出现「推荐里有 N 项、待审查里又出现同一首」。
    """
    rows = store.conn.execute(
        """SELECT s.id, s.provider, s.provider_id, s.score, l.rel_path,
                  ro.name AS root_name, r.candidate_json
           FROM staged_lyrics s
           JOIN locations l ON l.id = s.location_id
           JOIN roots ro ON ro.id = l.root_id
           LEFT JOIN resolutions r
                  ON r.id = (
                      SELECT r2.id FROM resolutions r2
                       WHERE r2.provider = s.provider
                         AND r2.provider_id = s.provider_id
                         AND r2.location_id = s.location_id
                       ORDER BY r2.updated_at DESC LIMIT 1
                  )
           WHERE s.state = 'needs_review'"""
    ).fetchall()
    safe: list[dict[str, Any]] = []
    for row in rows:
        info = _staged_reason(row["candidate_json"])
        failed_keys = {c.get("key") for c in (info.get("failed_checks") or []) if c.get("key")}
        # 只认「未通过的全是软门槛」。有硬闸门失败、或压根没有判定记录
        # （failed_keys 为空但 reason 也为空）都不算安全。
        if info.get("reason") is None:
            continue
        if not (failed_keys & _HARD_GATE_KEYS):
            safe.append({
                "id": row["id"],
                "rel_path": row["rel_path"],
                # 前端要按 root+path+来源取歌词预览，缺一不可
                "root": row["root_name"],
                "provider": row["provider"],
                "provider_id": row["provider_id"],
                "score": row["score"],
                "reason": info.get("reason"),
            })
    return safe


def _parse_ai_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _staged(
    store: StateStore,
    *,
    state_filter: str | None,
    page: int,
    size: int,
    by_track: bool = False,
    exclude_safe: bool = False,
    provider_order: list[str] | None = None,
) -> dict[str, Any]:
    where = "1=1" if not state_filter else "s.state = ?"
    params: list[Any] = [] if not state_filter else [state_filter]

    # 「推荐采纳」与「待审查」必须是两个互不重叠的桶：前者的条目不能
    # 又在后者出现，否则使用者会以为同一首歌有两份要处理。
    if exclude_safe:
        safe_ids = [item["id"] for item in _safe_batch_entries(store)]
        for start in range(0, len(safe_ids), 400):
            chunk = safe_ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            where += f" AND s.id NOT IN ({marks})"
            params.extend(chunk)

    # **按曲目分页**（待审查页用）。逐条分页会把同一首歌的不同候选切到两页：
    # 一首歌有 3 个来源候选时，第 1 页末尾与第 2 页开头各出现一次「同一首歌」，
    # 使用者会以为队列里有两首。所以先按曲目切页，再把这一页的曲目整批取出。
    page_paths: list[str] | None = None
    if by_track:
        all_paths = [
            str(row["rel_path"])
            for row in store.conn.execute(
                f"""SELECT l.rel_path AS rel_path FROM staged_lyrics s
                      JOIN locations l ON l.id = s.location_id
                     WHERE {where}
                     GROUP BY l.rel_path
                     ORDER BY MAX(s.created_at) DESC""",
                tuple(params),
            )
        ]
        total = len(all_paths)
        start = max(0, (page - 1) * size)
        page_paths = all_paths[start:start + size]
    else:
        total = store.conn.execute(
            f"SELECT COUNT(*) FROM staged_lyrics s WHERE {where}", tuple(params)
        ).fetchone()[0]

    extra = ""
    extra_params: list[Any] = []
    if page_paths:
        marks = ",".join("?" for _ in page_paths)
        extra = f" AND l.rel_path IN ({marks})"
        extra_params = list(page_paths)
    # 连带出判定依据：理由存在 resolutions.candidate_json 里，
    # 加一个 JOIN 就能拿到，**不需要对每一行再发一次请求**（N+1）。
    # 复核队列每一行都必须能直接说明「为什么没自动接受」——
    # 只显示一个匹配度数字，使用者根本无从判断。
    # 判定依据（reason）取自 resolutions.candidate_json。
    #
    # **写成两个标量子查询 + COALESCE，而不是 LEFT JOIN。**
    # 除了避免下面说的行放大，还有一个只在本项目的**实际运行引擎**上才会暴露的
    # 限制：容器里是 SQLite 3.46.1，子查询的 `ORDER BY` 里**不允许引用外层
    # 列**——`ORDER BY CASE WHEN r2.location_id = s.location_id ...` 会报
    # `no such column: s.location_id`（同一个查询在本机 macOS 的 SQLite 上
    # 正常）。这条 500 是这样被发现的：单元测试全绿，实际运行待审查页打不开。
    # 所以对这条 SQL 的改动必须**在容器里也跑一次**。
    #
    # **必须用相关子查询定位唯一一行，不能直接 JOIN。**
    # 实际：(provider, provider_id) 在 resolutions 里**不唯一**——同一个 QQ
    # 候选会被不同曲目分别解析一次（实际 `qq/0040T0EA00oi4h` 有 2 条，
    # 分别属于《龍捲風》与《存在》）。直接 JOIN 会把一条暂存记录放大成多行，
    # 界面上就是「QQ 音乐 0.60 / QQ 音乐 0.60」两个一模一样的标签，
    # **而且其中一次的判定依据来自另一首歌**。凡出现重复标签、或理由与曲目
    # 对不上，先怀疑这里。
    rows = store.conn.execute(
        f"""SELECT s.id, s.state, s.provider, s.provider_id, s.score, s.duration_delta,
                       s.ai_json,
                   s.content_hash, s.created_at, s.origin, l.rel_path,
                   ro.name AS root_name,
                   COALESCE(
                       -- 优先取**同一曲目**的那一条：同一个上游候选可能被多首
                       -- 歌分别解析过，只有 location 相同的那条理由才是本曲目的。
                       (SELECT r2.candidate_json FROM resolutions r2
                         WHERE r2.provider = s.provider
                           AND (r2.provider_id = s.provider_id
                                OR (r2.provider_id IS NULL AND s.provider_id IS NULL))
                           AND r2.location_id = s.location_id
                         ORDER BY r2.id DESC LIMIT 1),
                       -- 退回：不限定 location 的最新一条（兼容早期 location_id 为空的行）
                       (SELECT r3.candidate_json FROM resolutions r3
                         WHERE r3.provider = s.provider
                           AND (r3.provider_id = s.provider_id
                                OR (r3.provider_id IS NULL AND s.provider_id IS NULL))
                         ORDER BY r3.id DESC LIMIT 1)
                   ) AS candidate_json
            FROM staged_lyrics s
            JOIN locations l ON l.id = s.location_id
            JOIN roots ro ON ro.id = l.root_id
            WHERE {where}{extra}
            ORDER BY s.created_at DESC LIMIT ? OFFSET ?""",
        # 按曲目分页时**不能再按 size 截断**：页边界已经由 page_paths 定死，
        # 这里再 LIMIT size 会把同一页里第二首歌的候选截掉（实际只剩 1 组）。
        # SQLite 的 LIMIT -1 表示不限。
        (*params, *extra_params,
         -1 if page_paths is not None else size,
         0 if page_paths is not None else (page - 1) * size),
    ).fetchall() if (page_paths is None or page_paths) else []
    items = [
            {
                "id": r["id"], "state": r["state"], "provider": r["provider"],
                "root": r["root_name"],
                "provider_id": r["provider_id"], "score": r["score"],
                "duration_delta": r["duration_delta"], "rel_path": r["rel_path"],
                "at": r["created_at"],
                # 歌词的**获取方式**。使用者必须能区分源站原生 LRC 与
                # AI 对齐/生成——后者可信度完全不同。
                "origin": r["origin"],
                # AI 预审结论（未开启时为 None）：复核界面据此展示
                # 「模型认为是同一首，置信 0.9」这类建议
                "ai": _parse_ai_json(r["ai_json"]),
                **_staged_reason(r["candidate_json"]),
            }
            for r in rows
    ]

    # **按曲目聚合**。同一首歌在多个来源上各有一条记录，逐条列出会让
    # 使用者以为它们是不同的歌——实际队列里同一首《夜空中最亮的星》
    # 重复出现 3 次，而他真正要决定的是「这首歌用哪一份」。
    #
    # 保留 items（兼容既有消费方），另给 groups 供界面按曲目展示。
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    rank_of = {name: idx for idx, name in enumerate(provider_order or [])}
    for item in items:
        key = item["rel_path"]
        if key not in grouped:
            grouped[key] = {
                "rel_path": key,
                "root": item["root"],
                "best_id": item["id"],
                "best_score": item["score"],
                "candidates": [],
            }
            order.append(key)
        grp = grouped[key]
        grp["candidates"].append({
            "id": item["id"],
            "provider": item["provider"],
            "provider_id": item["provider_id"],
            "score": item["score"],
            "duration_delta": item["duration_delta"],
            # 来源优先顺序：分数相同时用它裁决（QQ 在前）
            "provider_rank": rank_of.get(item["provider"], len(rank_of)),
            "reason": item.get("reason") or (
                # 没有判定记录时**必须说明**，不能显示成空白——
                # 空白会被理解成「没有任何问题」，而实际是「没有记录」。
                None if item.get("has_evidence") else
                "无判定记录，重新刮削可生成。"
            ),
            "has_evidence": bool(item.get("has_evidence")),
            "ai": item.get("ai"),
            "failed_checks": item.get("failed_checks") or [],
            # 有 reason 才算有判定证据；旧快照可能两者皆空
            "safe": _is_safe_for_batch(
                item.get("failed_checks") or [],
                has_evidence=item.get("reason") is not None,
            ),
        })
        # 分数最高的那条代表这首歌，操作按钮默认作用于它
        if (item["score"] or 0) > (grp["best_score"] or 0):
            grp["best_id"] = item["id"]
            grp["best_score"] = item["score"]

    for grp in grouped.values():
        # 同曲目的候选按**分数降序**排列；分数相同才按来源优先顺序
        # （配置里的 `providers.enabled` 顺序，QQ 在前）。
        #
        # 为什么不再把「安全」的排前面：使用者要的就是「分数最高的那条」，
        # 实际案例里最优的 0.86 被当成不安全而推荐了 0.83 —— 看着像
        # 系统推荐错了。安全与否仍然逐条显示在界面上，由人决定采不采用。
        grp["candidates"].sort(
            key=lambda c: (
                -(c["score"] or 0),
                c.get("provider_rank", 99),
                c["id"],
            )
        )
        grp["safe_count"] = sum(1 for c in grp["candidates"] if c["safe"])

    return {
        "total": int(total),
        "page": page,
        "size": size,
        "items": items,
        "groups": [grouped[k] for k in order],
    }


#: 硬闸门。这几道全过，说明「确实是这首歌」；只有 confidence 是软门槛
#: （加权总分），它不达标只代表「不够确信」，不代表「可能是错的」。
_HARD_GATE_KEYS = frozenset({"duration_gate", "version", "title", "artist"})


def _is_safe_for_batch(
    failed_checks: list[dict[str, Any]], *, has_evidence: bool = True
) -> bool:
    """该候选是否属于「硬闸门全过、仅软门槛未达」。

    **必须有判定证据。** 旧快照里没有 `checks`，此时 `failed_checks` 为空——
    如果只看「没有硬闸门失败」就判为安全，那就是把**没有证据当成证据表明安全**。
    实际这个错误犯了两次：一次在统计脚本里（把 18 条缺证据的算成了 23 条安全），
    一次就在这个函数里（把分数 0.30 的旧候选标成了「可接受」）。

    与 `/api/v1/staged/safe-batch` 保持同一判据——两处各写一套的话，
    迟早出现「列表说可接受、批量又说不安全」的自相矛盾。
    """
    if not has_evidence:
        return False
    keys = {c.get("key") for c in failed_checks if c.get("key")}
    return not (keys & _HARD_GATE_KEYS)


def _set_staged(store: StateStore, staged_id: int, new_state: str) -> dict[str, Any]:
    row = store.conn.execute(
        "SELECT id FROM staged_lyrics WHERE id=?", (staged_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="不存在")
    store.set_staged_state(staged_id, new_state)
    return {"ok": True, "id": staged_id, "state": new_state}


def _issues(store: StateStore, *, page: int, size: int) -> dict[str, Any]:
    rows = store.conn.execute(
        """SELECT loc.rel_path, ro.name AS root_name, obs.embedded_state,
                  obs.sidecar_state, obs.sidecar_size, obs.sidecar_names_differ, obs.issues
           FROM lyric_observations obs
           JOIN locations loc ON loc.id = obs.location_id
           JOIN roots ro ON ro.id = loc.root_id
           WHERE obs.id IN (SELECT MAX(id) FROM lyric_observations GROUP BY location_id)
             AND loc.missing_since IS NULL
             AND obs.issues IS NOT NULL AND obs.issues != '[]'
           ORDER BY loc.rel_path LIMIT ? OFFSET ?""",
        (size, (page - 1) * size),
    ).fetchall()
    return {
        "page": page,
        "size": size,
        "items": [
            {
                "rel_path": r["rel_path"], "root": r["root_name"],
                "embedded_state": r["embedded_state"], "sidecar_state": r["sidecar_state"],
                "sidecar_size": r["sidecar_size"],
                "sidecar_names_differ": bool(r["sidecar_names_differ"]),
                "issues": json.loads(r["issues"] or "[]"),
            }
            for r in rows
        ],
    }


def serve(cfg: Config) -> None:
    """启动 Web 服务（阻塞）。"""
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.web.bind,
        port=cfg.web.port,
        log_level="info",
        access_log=False,  # 访问日志默认关闭：URL 里可能含路径信息
    )
