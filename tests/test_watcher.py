"""递归监听的测试。

为什么测这些：

监听是「自动刮削」的入口，它出的错都很隐蔽——不会报错，只会**悄悄漏掉文件**
或**早早处理半截文件**。下面每条断言对应一个已经实际确认过的承诺：

- **慢速复制必须被延后。** 大文件拷贝中途 stat 已经在变，若不等稳定，
  会读到不完整的标签并把错误的元数据写进负缓存。
- **只满足「采样次数」或「时长」之一都不够。** 两者必须同时满足，
  否则极短间隔的连续采样会骗过判定。
- **整目录移入必须被发现。** 用户把一张专辑整个拖进库是最常见的操作，
  只为启动时已存在的目录注册任务是不够的。
- **自己产生的事件不能形成循环。** 事务临时文件必须被忽略规则排掉。
- **对账必须幂等。** 重复对账在没有变化时不应产生任何「变化」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.config import Config, RootConfig, StorageConfig, WatchConfig
from lyra.models import RootRole
from lyra.watcher import LibraryWatcher, StabilityGate

# ---------------------------------------------------------------------------
# 稳定性判定
# ---------------------------------------------------------------------------


def test_稳定判定需要同时满足两个条件(tmp_path: Path):
    """只满足「时长」或只满足「采样次数」都不算稳定。

    这是防御「极短间隔连续采样」与「恰好卡在阈值」两类误判的关键。
    """
    gate = StabilityGate(stable_seconds=60.0, stable_samples=3)
    target = tmp_path / "a.flac"
    target.write_bytes(b"x" * 1000)
    gate.note(target, now=0.0)

    assert gate.observe(target, now=0.0) is False, "首次采样只是建立基线"
    # 时长够了但采样次数不够 → 仍不稳定
    assert gate.observe(target, now=100.0) is False, "采样次数不足，不得提前放行"
    # 两者都满足
    assert gate.observe(target, now=200.0) is True
    assert gate.pending_count() == 0, "稳定后必须从待判定集合移除"


def test_文件持续变化时永远不稳定(tmp_path: Path):
    """慢速复制：每次采样文件都在变大，必须一直等下去。

    若这里出错，Lyra 会把拷贝到一半的文件当成完整音乐处理。
    """
    gate = StabilityGate(stable_seconds=10.0, stable_samples=2)
    target = tmp_path / "growing.flac"
    target.write_bytes(b"x" * 100)
    gate.note(target, now=0.0)

    for step in range(6):
        with open(target, "ab") as fh:
            fh.write(b"y" * 500)
        assert gate.observe(target, now=step * 30.0) is False, (
            f"第 {step} 次采样时文件仍在变化，绝不能判定为稳定"
        )
    assert gate.pending_count() == 1


def test_文件在等待期间消失则丢弃(tmp_path: Path):
    gate = StabilityGate(stable_seconds=0.0, stable_samples=1)
    target = tmp_path / "gone.flac"
    target.write_bytes(b"x")
    gate.note(target, now=0.0)
    target.unlink()
    assert gate.observe(target, now=1.0) is False
    assert gate.pending_count() == 0, "消失的文件不得留在待判定集合里"


def test_符号链接不被当作可处理文件(tmp_path: Path):
    gate = StabilityGate(stable_seconds=0.0, stable_samples=1)
    real = tmp_path / "real.flac"
    real.write_bytes(b"x")
    link = tmp_path / "link.flac"
    link.symlink_to(real)
    gate.note(link, now=0.0)
    assert gate.observe(link, now=1.0) is False


# ---------------------------------------------------------------------------
# 监听器
# ---------------------------------------------------------------------------


@pytest.fixture
def watcher_env(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="main", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        # 把稳定窗口压到最小，让测试跑得快；语义与生产一致
        watch=WatchConfig(
            stable_seconds=0.05, stable_samples=2, reconcile_interval_seconds=900
        ),
    )
    batches: list[list[Path]] = []
    watcher = LibraryWatcher(
        cfg, cfg.roots[0], on_files=lambda paths: batches.append(paths)
    )
    return lib, cfg, watcher, batches


def _make_flac(path: Path) -> None:
    """造一个最小的可被识别为音频的文件。

    只用于「是否进入处理流程」的判定，因此不必是合法 FLAC——
    监听层按扩展名初筛，真正的容器判定在 formats 层。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + b"\x00" * 64)


def test_稳定后才回调且只回调一次(watcher_env):
    lib, _cfg, watcher, batches = watcher_env
    target = lib / "a.flac"
    _make_flac(target)

    watcher._gate.note(target, now=0.0)  # noqa: SLF001
    assert watcher.tick(now=0.0) == [], "第一次采样只建立基线"
    ready = watcher.tick(now=1.0)
    assert [p.name for p in ready] == ["a.flac"]
    assert len(batches) == 1
    # 再采样不应重复回调
    assert watcher.tick(now=2.0) == []
    assert len(batches) == 1, "同一个文件不得重复触发处理"


def test_整目录移入时其内文件被发现(watcher_env, tmp_path: Path):
    """用户把一张专辑整个拖进库是最常见的操作。"""
    lib, _cfg, watcher, batches = watcher_env
    incoming = tmp_path / "incoming" / "专辑A"
    _make_flac(incoming / "01 歌曲.flac")
    _make_flac(incoming / "02 歌曲.flac")
    _make_flac(incoming / "cover.jpg")  # 非音频，不该进流程

    watcher._enqueue_directory(incoming, now=0.0)  # noqa: SLF001
    assert watcher._gate.pending_count() == 2  # noqa: SLF001

    watcher.tick(now=0.0)
    ready = watcher.tick(now=1.0)
    assert sorted(p.name for p in ready) == ["01 歌曲.flac", "02 歌曲.flac"]


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("a.flac", True),
        ("a.ogg", True),
        ("a.mp3", True),
        ("a.tmp", False),
        ("a.flac.crdownload", False),
        ("a.part", False),
        (".a.flac.lyric-txn-abc.tmp", False),
        ("cover.jpg", False),
        ("x.m3u8", False),
        ("._a.flac", False),
        (".DS_Store", False),
    ],
)
def test_事件接受规则(watcher_env, name: str, accepted: bool):
    """本服务自己的事务临时文件必须被排掉，否则会形成自我触发的循环。"""
    lib, _cfg, watcher, _batches = watcher_env
    assert watcher._accept(lib / name) is accepted  # noqa: SLF001


def test_受管根之外的事件被拒绝(watcher_env, tmp_path: Path):
    _lib, _cfg, watcher, _batches = watcher_env
    outside = tmp_path / "outside" / "a.flac"
    _make_flac(outside)
    assert watcher._accept(outside) is False  # noqa: SLF001


def test_对账发现新增文件且重复对账幂等(watcher_env):
    """对账是漏事件的兜底，但自身不能制造重复工作。"""
    lib, _cfg, watcher, _batches = watcher_env
    _make_flac(lib / "one.flac")

    changed = watcher.reconcile(now=0.0)
    assert [p.name for p in changed] == ["one.flac"]

    again = watcher.reconcile(now=1.0)
    assert again == [], "签名未变时对账不得报告变化"

    _make_flac(lib / "two.flac")
    changed2 = watcher.reconcile(now=2.0)
    assert [p.name for p in changed2] == ["two.flac"]


def test_对账能发现停机期间被修改的文件(watcher_env):
    lib, _cfg, watcher, _batches = watcher_env
    target = lib / "one.flac"
    _make_flac(target)
    watcher.reconcile(now=0.0)

    # 模拟服务停机期间外部工具改了文件（mtime/大小变化）
    target.write_bytes(b"fLaC" + b"\x11" * 128)
    changed = watcher.reconcile(now=1.0)
    assert [p.name for p in changed] == ["one.flac"]


def test_每日完整核对到期判定(watcher_env):
    _lib, cfg, watcher, _batches = watcher_env
    assert watcher.full_scan_due() is False, "刚启动不应立即触发每日核对"
    assert watcher._next_full_scan > watcher._clock()  # noqa: SLF001


def test_受管根不可用时降级而不是假装正常(tmp_path: Path):
    """监听不可用必须如实报告，绝不据此认为「全库被删除」。"""
    cfg = Config(
        roots=[RootConfig(name="gone", role=RootRole.LIBRARY, path=str(tmp_path / "nope"))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    watcher = LibraryWatcher(cfg, cfg.roots[0], on_files=lambda _p: None)
    watcher.start()
    assert watcher.stats.degraded is True
    assert "受管根不可用" in watcher.stats.degrade_reason
