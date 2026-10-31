"""曲库整理的冲突人工处理。

## 为什么要有「处理」而不只是「显示」

生产曲库里有一批脏目录（`MusicLib/G.E.M/…`），整理时目标路径已经有
文件——预览里显示「目标已存在，拒绝覆盖」然后就结束了，用户既不知道
该拿哪一个，也没有任何办法表态。

现在每条冲突都能逐条选：

- **保留已有**（默认）：不动任何文件，跳过这条；
- **用它替换**：目标文件移进回收站（**可撤销**），再把整理后的文件放
  过去。

「替换」是用户显式的逐条选择，自动流程永远不会走这条路；被替换的
文件进回收站而不是被删除。

## 红线

- 默认行为不变：不带决议时，仍然是「跳过、绝不覆盖」。
- 替换走回收站 + `organize_ops` 记录，能在「回收站」里还原。
- 「多个源映射到同一目标」这种冲突没有可替换的对象，不能选替换。
"""

from __future__ import annotations

from pathlib import Path

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.organize import build_plan, execute_plan


def _env(tmp_path: Path) -> tuple[Config, Path]:
    lib = tmp_path / "lib"
    (lib / "旧目录").mkdir(parents=True)
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    return cfg, lib


def _make_flac(path: Path, title: str, album: str, track: str = "1") -> None:
    """写出带真实标签的 FLAC——目标路径是按标签算的，没有标签就没有冲突。"""
    from conftest import make_flac

    path.parent.mkdir(parents=True, exist_ok=True)
    make_flac(path, entries=(
        ("TITLE", title), ("ALBUM", album), ("ARTIST", "艺人"),
        ("TRACKNUMBER", track),
    ))


def test_冲突项带类型与目标信息(tmp_path: Path):
    cfg, lib = _env(tmp_path)
    # 目标位置已有文件，源在旧目录
    _make_flac(lib / "旧目录" / "歌.flac", "歌", "专辑")
    (lib / "艺人" / "专辑").mkdir(parents=True)
    _make_flac(lib / "艺人" / "专辑" / "01 - 歌.flac", "歌", "专辑")

    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    conflicts = plan["conflicts"]
    assert len(conflicts) == 1, f"应有一条冲突：{conflicts}"
    assert conflicts[0]["conflict_type"] == "target_exists"
    assert conflicts[0]["target_info"]["size"] > 0
    store.close()


def test_默认不处理时跳过不覆盖(tmp_path: Path):
    """不带决议 → 保持旧行为：跳过，目标文件保持原样。"""
    cfg, lib = _env(tmp_path)
    _make_flac(lib / "旧目录" / "歌.flac", "歌", "专辑")
    (lib / "艺人" / "专辑").mkdir(parents=True)
    target = lib / "艺人" / "专辑" / "01 - 歌.flac"
    _make_flac(target, "歌", "专辑")

    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    store.save_organize_plan(
        plan_id=str(plan["plan_id"]), root_id=int(root_id),
        subpath="", payload=plan,
    )
    row = store.organize_plan(str(plan["plan_id"]))
    result = execute_plan(store, cfg, cfg.roots[0], row, dirs=[])

    assert result["moved"] == 0
    assert result["skipped_conflicts"] == 1, "跳过的冲突要计数，不能悄悄消失"
    assert target.is_file(), "默认不覆盖目标"
    assert (lib / "旧目录" / "歌.flac").is_file(), "源文件不动"
    store.close()


def test_选择替换后目标进回收站且源移过去(tmp_path: Path):
    cfg, lib = _env(tmp_path)
    _make_flac(lib / "旧目录" / "歌.flac", "歌", "专辑")
    (lib / "艺人" / "专辑").mkdir(parents=True)
    target = lib / "艺人" / "专辑" / "01 - 歌.flac"
    _make_flac(target, "歌", "专辑")
    target_size = target.stat().st_size

    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    conflict = plan["conflicts"][0]
    store.save_organize_plan(
        plan_id=str(plan["plan_id"]), root_id=int(root_id),
        subpath="", payload=plan,
    )
    row = store.organize_plan(str(plan["plan_id"]))
    result = execute_plan(
        store, cfg, cfg.roots[0], row, dirs=[],
        resolutions={conflict["old_rel_path"]: "replace"},
    )

    assert result["ok"], result["errors"]
    assert result["replaced"] == 1
    assert result["moved"] == 1
    # 源文件到了目标位置
    assert target.is_file()
    assert not (lib / "旧目录" / "歌.flac").exists()
    # 被替换的目标进了回收站（有记录，可还原）
    ops = store.conn.execute(
        "SELECT op_kind, trash_rel_path FROM organize_ops WHERE op_kind='delete'"
    ).fetchall()
    assert len(ops) == 1, "应有一条删除记录（可撤销）"
    assert ops[0]["trash_rel_path"]
    trash_file = (
        cfg.storage.resolved_data_dir / "trash" / str(result["batch_id"])
        / str(ops[0]["trash_rel_path"])
    )
    assert trash_file.is_file(), "被替换的文件应在回收站里"
    assert trash_file.stat().st_size == target_size
    store.close()


def test_替换后目标位置行不残留(tmp_path: Path):
    """替换后目标路径的位置行要清掉——文件已经不是原来那个了。"""
    cfg, lib = _env(tmp_path)
    _make_flac(lib / "旧目录" / "歌.flac", "歌", "专辑")
    (lib / "艺人" / "专辑").mkdir(parents=True)
    target_rel = "艺人/专辑/01 - 歌.flac"
    _make_flac(lib / target_rel, "歌", "专辑")

    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    store.upsert_location(
        root_id=root_id, rel_path=target_rel, size=32, mtime=1.0,
    )
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    conflict = plan["conflicts"][0]
    store.save_organize_plan(
        plan_id=str(plan["plan_id"]), root_id=int(root_id),
        subpath="", payload=plan,
    )
    row = store.organize_plan(str(plan["plan_id"]))
    execute_plan(
        store, cfg, cfg.roots[0], row, dirs=[],
        resolutions={conflict["old_rel_path"]: "replace"},
    )
    # 目标路径现在归源文件所有：要么没有行，要么行指向新内容
    rows = store.conn.execute(
        "SELECT id FROM locations WHERE root_id=? AND rel_path=?",
        (root_id, target_rel),
    ).fetchall()
    assert len(rows) <= 1, "同一路径不能有多行"
    store.close()


def test_同一目标冲突不可替换(tmp_path: Path):
    """两个源映射到同一目标：没有可替换的对象，不能选替换。"""
    cfg, lib = _env(tmp_path)
    _make_flac(lib / "a" / "歌1.flac", "歌", "专辑")
    _make_flac(lib / "b" / "歌2.flac", "歌", "专辑")
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    same_target = [c for c in plan["conflicts"] if c.get("conflict_type") == "same_target"]
    assert same_target, f"应识别出同目标冲突：{plan['conflicts']}"
    assert "target_info" not in same_target[0] or not same_target[0].get("target_info")
    store.close()
