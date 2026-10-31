"""递归监听：事件驱动 + 稳定性判定 + 周期对账 + 每日全量核对。

设计依据：

1. **不能只靠事件。** inotify 会丢事件（队列溢出、服务重启、SMB 写入时序），
   因此必须有周期对账兜底，以及每日一次完整核对。这不是可选项。
2. **不能一有事件就处理。** 拷贝一张专辑会触发大量 CREATE/MODIFY，且大文件
   在拷贝中途不可读。必须等文件稳定（大小与 mtime 连续多次不变 + 可打开）。
3. **新目录与移入的整棵子树必须被发现**，而不是只为启动时已存在的目录注册任务。
4. **自己产生的事件不能形成循环。** 本服务的事务临时文件与 sidecar 都由
   `scan` 的忽略规则排掉；sidecar 变化只唤醒本地冲突检查，不直接请求网络。
5. **监听失效不能被解释成「全库删除了」。** 监听不可用时降级为轮询并如实报告，
   绝不据此删除任何状态。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import scan
from .config import Config, RootConfig
from .formats import AUDIO_EXTENSIONS

__all__ = ["StabilityGate", "LibraryWatcher", "WatchStats"]

LogFn = Callable[[str], None]
BatchFn = Callable[[list[Path]], None]


def _noop(_: str) -> None:
    return


@dataclass(slots=True)
class WatchStats:
    events_seen: int = 0
    stable_batches: int = 0
    files_processed: int = 0
    reconciles: int = 0
    full_scans: int = 0
    degraded: bool = False
    _degrade_reason: str = ""

    @property
    def degrade_reason(self) -> str:
        return self._degrade_reason


# ---------------------------------------------------------------------------
# 稳定性判定
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Sample:
    size: int
    mtime_ns: int
    first_seen: float
    unchanged: int = 1


class StabilityGate:
    """等文件稳定后再处理。

    判据同时要求：
      - 连续 `stable_samples` 次采样（大小, mtime_ns）完全不变
      - 首次观察到**当前签名**至今 >= `stable_seconds`

    只满足其一都不够：单看次数会被「间隔极短的连续采样」骗过；
    单看时长则可能在恰好等于阈值时误判。
    """

    def __init__(self, stable_seconds: float, stable_samples: int) -> None:
        self._stable_seconds = max(0.0, float(stable_seconds))
        self._stable_samples = max(1, int(stable_samples))
        self._samples: dict[Path, _Sample] = {}

    def pending_count(self) -> int:
        return len(self._samples)

    def pending_paths(self) -> list[Path]:
        return sorted(self._samples)

    def forget(self, path: Path) -> None:
        self._samples.pop(path, None)

    def clear(self) -> None:
        self._samples.clear()

    def note(self, path: Path, *, now: float) -> None:
        """登记一个待判定的文件（事件到达时调用）。"""
        self._samples.setdefault(
            path,
            _Sample(
                size=-1,
                mtime_ns=-1,
                first_seen=now,
                unchanged=0,
            ),
        )

    def observe(self, path: Path, *, now: float) -> bool:
        """采样一次；返回 True 表示该文件已经稳定，可以处理。

        文件消失、不可读、签名变化都会返回 False，并重置计时。
        """
        sample = self._samples.get(path)
        if sample is None:
            return False

        try:
            stat = path.stat()
        except OSError:
            self._samples.pop(path, None)
            return False

        if not path.is_file() or path.is_symlink():
            self._samples.pop(path, None)
            return False

        # 必须能真正打开：拷贝中的大文件 stat 可能不变但仍不可读
        try:
            with open(path, "rb") as fh:
                fh.read(4)
        except OSError:
            sample.first_seen = now
            sample.unchanged = 0
            return False

        signature = (stat.st_size, stat.st_mtime_ns)
        if (sample.size, sample.mtime_ns) != signature:
            # 签名变化 → 重新计时，慢速复制因此被自动延后
            sample.size, sample.mtime_ns = signature
            sample.first_seen = now
            sample.unchanged = 1
            return False

        sample.unchanged += 1
        elapsed = now - sample.first_seen
        if sample.unchanged >= self._stable_samples and elapsed >= self._stable_seconds:
            self._samples.pop(path, None)
            return True
        return False


# ---------------------------------------------------------------------------
# watchdog 事件收集
# ---------------------------------------------------------------------------


class _Collector(FileSystemEventHandler):
    def __init__(self, watcher: LibraryWatcher) -> None:
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        # **绝不让异常逃出去。**
        #
        # watchdog 的 `dispatch_events` 不捕获 handler 的异常——一个事件
        # 让处理抛错，emitter 线程就**静默死掉**：inotify 的 fd 还在，
        # 但再也不会派发任何事件。现场症状是「新建的文件永远不被处理、
        # 删除的文件永远不被发现」，而日志里什么异常都看不到。
        #
        # 这里兜住异常并记一笔，emitter 线程就能一直活着。
        try:
            self._watcher._on_event(event)  # noqa: SLF001 — 同模块协作
        except Exception as exc:  # noqa: BLE001 — 单个事件不能杀死监听线程
            self._watcher._log(
                f"[watch] 处理文件事件失败（{type(exc).__name__}: {exc}），已忽略"
            )


#: 只关心**会改变内容或位置**的事件。
#:
#: 这份白名单是必需的，不是优化：稳定性判定要 `open()` 文件读取头部，
#: 而 inotify 会把 `IN_ACCESS` / `IN_OPEN` 也报上来。若照单全收，
#: 「检查稳定」本身就会产生事件 → 文件被重新记入待判定 → 再次检查 →
#: 又产生事件，形成**自我触发的死循环**：同一批文件会被无限重复处理。
#: 实际症状是 205 个文件产生 2870 个事件、回调以同样 205 反复触发。
_WATCHED_EVENT_TYPES: frozenset[str] = frozenset(
    {"created", "modified", "moved", "deleted", "closed"}
)


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class LibraryWatcher:
    """一个受管根的递归监听器。

    `on_files` 收到的是**已稳定**的音频文件批次；
    `on_reconcile` / `on_full_scan` 用于触发上层对账（可选）。
    """

    def __init__(
        self,
        cfg: Config,
        root: RootConfig,
        on_files: BatchFn,
        *,
        on_reconcile: BatchFn | None = None,
        on_full_scan: BatchFn | None = None,
        on_deleted: Callable[[Path], None] | None = None,
        log: LogFn = _noop,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.base = root.resolved_path
        self.stats = WatchStats()
        self._on_files = on_files
        self._on_reconcile = on_reconcile
        self._on_full_scan = on_full_scan
        self._on_deleted = on_deleted
        self._log = log
        self._clock = clock

        watch = cfg.watch
        self._gate = StabilityGate(watch.stable_seconds, watch.stable_samples)
        self._observer: Observer | None = None
        self._manifest: dict[str, tuple[int, int]] = {}
        # 记录「上一次处理完成后」的文件签名，用于吸收自身写入产生的事件
        self._processed: dict[Path, tuple[int, int]] = {}
        self._last_reconcile = clock()
        self._next_full_scan = self._compute_next_full_scan()
        self._lock = threading.Lock()

    # -- 事件 -------------------------------------------------------------

    def _accept(self, path: Path) -> bool:
        """事件目标是否值得我们关心。"""
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            return False
        if scan.is_ignored(path.name, extra_patterns=tuple(self.root.ignore_globs)):
            return False
        if any(part in scan.IGNORE_DIR_NAMES or part in set(self.root.ignore_dirs)
               for part in path.parts):
            return False
        # 必须在受管根内（防止软链/越界）
        try:
            path.relative_to(self.base)
        except ValueError:
            return False
        return True

    def _on_event(self, event: FileSystemEvent) -> None:
        # 只处理会改变内容或位置的事件；`opened` / `closed_no_write` / `accessed`
        # 必须忽略，否则稳定性检查自己的读取动作会把自己重新唤醒（见上方白名单注释）
        if getattr(event, "event_type", "") not in _WATCHED_EVENT_TYPES:
            return

        now = self._clock()
        with self._lock:
            self.stats.events_seen += 1
            dest = getattr(event, "dest_path", "") or ""
            if getattr(event, "event_type", "") == "deleted":
                # **文件被删除：立刻告诉上层。**
                #
                # 原来删除事件被当成「文件变化」处理——进稳定判定队列，
                # 而队列里它已经 stat 不到文件，于是被静默跳过。位置行
                # 永远不被标记缺失，待办里的条目就一直挂着（用户反馈：
                # 「我删除了文件，为什么不会自动更新消失」）。
                #
                # 现在直接回调：上层把位置标记为缺失，待办随之消失。
                for target in (dest, event.src_path):
                    if not target:
                        continue
                    path = Path(str(target))
                    if not self._accept(path):
                        continue
                    self._gate.forget(path)
                    if self._on_deleted is not None:
                        try:
                            self._on_deleted(path)
                        except Exception as exc:  # noqa: BLE001 — 单个失败不影响监听
                            self._log(f"[watch] 处理删除事件失败 {path.name}: {exc}")
                return
            if event.is_directory:
                # 新建目录或整目录移入：枚举其中的音频，交给稳定判定。
                # 空目录事件与稍后到达的文件事件都要处理，所以这里只是提前发现。
                for target in (dest, event.src_path):
                    if target:
                        self._enqueue_directory(Path(str(target)), now=now)
                return
            for target in (dest, event.src_path):
                if not target:
                    continue
                path = Path(str(target))
                if not self._accept(path):
                    continue
                # 若当前签名正是「我们自己上一次处理后留下的签名」，
                # 说明这是我们自己写入产生的事件，吸收掉而不是重新处理。
                if self._is_self_inflicted(path):
                    continue
                self._gate.note(path, now=now)

    def _is_self_inflicted(self, path: Path) -> bool:
        recorded = self._processed.get(path)
        if recorded is None:
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        return recorded == (stat.st_size, stat.st_mtime_ns)

    def _enqueue_directory(self, directory: Path, *, now: float) -> None:
        if not directory.is_dir():
            return
        for path in scan.iter_audio_files(directory, self.root):
            self._gate.note(path, now=now)

    # -- 周期性工作 -------------------------------------------------------

    def tick(self, *, now: float | None = None) -> list[Path]:
        """采样所有待判定文件，返回本轮已经稳定的批次。"""
        now = self._clock() if now is None else now
        with self._lock:
            ready: list[Path] = []
            for path in self._gate.pending_paths():
                if self._gate.observe(path, now=now):
                    ready.append(path)
        if not ready:
            return []

        # 回调在锁外执行：它可能联网、写文件，耗时不可控，不能占着锁。
        self._on_files(ready)

        with self._lock:
            self.stats.stable_batches += 1
            self.stats.files_processed += len(ready)
            for path in ready:
                # 回调写入了 sidecar/内嵌歌词，因此：
                #   1) 丢弃回调期间被自身写入事件重新记入的待判定项
                #      —— 这些文件刚刚处理过，不该再排一次队
                #   2) 记录**写入之后**的最终签名，供之后识别并吸收自身事件
                #      （必须在回调之后记录：写入会改变 mtime 与大小）
                self._gate.forget(path)
                try:
                    stat = path.stat()
                    self._processed[path] = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    self._processed.pop(path, None)
        return ready

    def reconcile(self, *, now: float | None = None) -> list[Path]:
        """全量对账：发现事件丢失、服务停机期间的变化。

        只把「新增或签名变化」的文件交给稳定判定，因此对账本身不会
        造成重复请求——真正的去重由负缓存与幂等写入负责。
        """
        now = self._clock() if now is None else now
        with self._lock:
            self.stats.reconciles += 1
            changed: list[Path] = []
            manifest: dict[str, tuple[int, int]] = {}
            for path in scan.iter_audio_files(self.base, self.root):
                try:
                    stat = path.stat()
                    rel = str(path.relative_to(self.base))
                except OSError:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                manifest[rel] = signature
                if self._manifest.get(rel) != signature:
                    changed.append(path)
            self._manifest = manifest
            for path in changed:
                self._gate.note(path, now=now)
        if changed:
            self._log(f"[watch] 对账发现 {len(changed)} 个新增/变化文件")
            if self._on_reconcile is not None:
                self._on_reconcile(changed)
        return changed

    def _compute_next_full_scan(self, *, now: float | None = None) -> float:
        """下一次每日完整核对的**单调时钟**时刻。

        用单调时钟做间隔、用配置时区算墙钟时刻，避免系统校时引发风暴。
        """
        try:
            tz = ZoneInfo(self.cfg.watch.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        raw = self.cfg.watch.full_scan_local_time
        try:
            hour, minute = (int(x) for x in raw.split(":", 1))
        except (ValueError, AttributeError):
            hour, minute = 3, 30

        wall = datetime.now(tz)
        target = wall.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= wall:
            target += timedelta(days=1)
        return (self._clock() if now is None else now) + (target - wall).total_seconds()

    def full_scan_due(self, *, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return now >= self._next_full_scan

    def run_full_scan(self, *, now: float | None = None) -> list[Path]:
        now = self._clock() if now is None else now
        with self._lock:
            self.stats.full_scans += 1
            self._manifest.clear()  # 强制全量重新比对
        changed = self.reconcile(now=now)
        self._next_full_scan = self._compute_next_full_scan(now=now)
        self._log("[watch] 每日完整核对完成")
        if self._on_full_scan is not None:
            self._on_full_scan(changed)
        return changed

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """启动递归监听。失败时降级为轮询并如实记录，不假装正常。"""
        if not self.base.is_dir():
            self._degrade(f"受管根不可用: {self.base}")
            return
        try:
            observer = Observer(timeout=1.0)
            observer.schedule(_Collector(self), str(self.base), recursive=True)
            observer.start()
        except Exception as exc:  # 包括 inotify watch 限额耗尽
            self._degrade(f"监听启动失败（{type(exc).__name__}: {exc}），降级为轮询")
            return
        self._observer = observer
        self._log(f"[watch] 已递归监听 {self.base}")

    def observer_alive(self) -> bool:
        """监听线程是否还在工作。

        watchdog 的 emitter 线程一旦因 handler 异常而死，fd 还在但不再
        派发事件——从外面看「一切正常」，实际监听已经失明。
        """
        observer = self._observer
        return observer is not None and observer.is_alive()

    def restart_observer(self) -> bool:
        """重建监听。返回是否重建成功。"""
        if self.observer_alive():
            return False
        self._log("[watch] 监听线程已停止，正在重启")
        self.stop()
        self.start()
        return True

    def _degrade(self, reason: str) -> None:
        self.stats.degraded = True
        self.stats._degrade_reason = reason  # noqa: SLF001
        self._log(f"[watch] {reason}")

    def stop(self) -> None:
        if self._observer is not None:
            with contextlib.suppress(Exception):
                self._observer.stop()
                self._observer.join(timeout=5)
            self._observer = None

    def run_forever(self, stop_event: threading.Event) -> None:
        """常驻循环：采样 → 周期对账 → 每日完整核对。

        注意即使监听降级为轮询，这里也会持续做对账，
        因此漏事件仍会被补回来（第 6 条）。
        """
        self.start()
        self.reconcile()
        tick = max(1.0, min(5.0, self.cfg.watch.stable_seconds / 4 or 1.0))
        try:
            while not stop_event.wait(tick):
                self.tick()
                now = self._clock()
                if now - self._last_reconcile >= self.cfg.watch.reconcile_interval_seconds:
                    self._last_reconcile = now
                    self.reconcile(now=now)
                if self.full_scan_due(now=now):
                    self.run_full_scan(now=now)
        finally:
            self.stop()
