"""整理页预览与实际归位必须算出**同一个**目标路径。

## 真实故障（生产实测）

同一个文件被反复搬动（线上实测 `08 - 白色風車.flac` 被移动 8 次），
待办里的「待移动」永远消不下去。

根因是两边读标签的方式不同：

- **预览**（`build_plan`）用 `read_all_tags`——读全部标签，看得到
  `TOTALDISCS=2` → 判定多碟 → 目标文件名带 `1-` 碟号前缀；
- **归位**（`_organize_new_files`）用 `read_audio_meta`——只读**匹配白名单**
  （故意精简），里面没有 TOTALDISCS → 判定单碟 → 目标文件名不带前缀。

于是：预览说「该移到 A」，归位说「该移到 B」，两边轮流改对方的结果。

这个测试固定住：**两个入口对同一文件必须算出同一条路径**。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_flac

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.organize import build_plan, read_organize_tags, target_rel_path


def _multi_disc_flac(path: Path, *, total_discs: str = "2") -> None:
    """写一个带碟数信息的 FLAC。

    `TOTALDISCS` 决定了文件名要不要带 `1-` 前缀，正是震荡的判据。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    make_flac(path, entries=(
        ("TITLE", "白色風車"),
        ("ARTIST", "周杰倫"),
        ("ALBUM", "依然范特西"),
        ("TRACKNUMBER", "8"),
        ("DISCNUMBER", "1"),
        ("TOTALDISCS", total_discs),
        ("TRACKTOTAL", "10"),
        ("TOTALTRACKS", "10"),
    ))


def test_全量标签能读到结构字段(tmp_path: Path):
    """白名单读不到 TOTALDISCS；全量读取必须能读到。"""
    p = tmp_path / "歌.flac"
    _multi_disc_flac(p)
    tags = read_organize_tags(p)
    assert tags.get("TOTALDISCS") == ["2"], f"应读到碟数：{tags}"
    assert tags.get("TRACKTOTAL") == ["10"]


def test_多碟文件算出的目标带碟号前缀(tmp_path: Path):
    p = tmp_path / "08 - 白色風車.flac"
    _multi_disc_flac(p)
    tags = read_organize_tags(p)
    rel, _fields = target_rel_path(tags=tags, source=p)
    assert rel.endswith("/1-08 - 白色風車.flac"), f"多碟应带前缀：{rel}"


def test_白名单标签会算出不同路径_这就是震荡的来源(tmp_path: Path):
    """固定住「必须用全量标签」这个事实。

    旧实现里归位用 `read_audio_meta`（匹配白名单），它读不到 TOTALDISCS，
    于是把多碟专辑当成单碟、算出不带 `1-` 前缀的目标名；预览用全量标签，
    算出的却带前缀。两边各算各的 → 同一个文件被反复搬动。
    """
    from lyra.formats import read_audio_meta

    p = tmp_path / "08 - 白色風車.flac"
    _multi_disc_flac(p)

    full_tags = read_organize_tags(p)
    full_target, _ = target_rel_path(tags=full_tags, source=p)

    meta = read_audio_meta(p, tmp_path)
    assert "TOTALDISCS" not in meta.tags, "白名单本就不含碟数——这正是问题"
    narrow_target, _ = target_rel_path(tags=meta.tags, source=p)

    assert full_target != narrow_target, (
        "如果两者相同，说明白名单已经含结构字段，这个回归测试需要重写"
    )
    assert full_target.endswith("1-08 - 白色風車.flac")
    assert narrow_target.endswith("08 - 白色風車.flac")


def test_预览与归位算出同一条路径(tmp_path: Path):
    """**核心回归**：两个入口对同一文件必须给出相同的目标路径。

    不一致时，同一个文件会被两边轮流搬动，待办里的「待移动」永远
    消不下去（生产实测同一个文件被移动 8 次）。
    """
    lib = tmp_path / "lib"
    _multi_disc_flac(lib / "周杰倫" / "依然范特西" / "08 - 白色風車.flac")
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.organize.template = "{artist}/{album}/{track_no} - {title}"
    store = open_store(cfg.storage.resolved_data_dir)
    store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )

    # 预览用的路径
    plan = build_plan(store, cfg, cfg.roots[0], subpaths=[], exclude=[])
    preview_target = plan["moves"][0]["new_rel_path"] if plan["moves"] else ""

    # 归位用的路径（ingest 的算法：全量标签 + 同一个模板）
    src = lib / "周杰倫" / "依然范特西" / "08 - 白色風車.flac"
    tags = read_organize_tags(src)
    ingest_target, _ = target_rel_path(
        tags=tags, source=src, template=cfg.policy.organize.template
    )

    assert preview_target == ingest_target, (
        f"预览与归位算出不同路径：\n  预览 {preview_target}\n  归位 {ingest_target}"
    )
    store.close()


def test_单碟文件不会加前缀(tmp_path: Path):
    p = tmp_path / "08 - 白色風車.flac"
    _multi_disc_flac(p, total_discs="1")
    tags = read_organize_tags(p)
    rel, _fields = target_rel_path(tags=tags, source=p)
    assert rel.endswith("/08 - 白色風車.flac"), f"单碟不该带前缀：{rel}"
