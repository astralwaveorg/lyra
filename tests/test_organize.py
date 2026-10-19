"""曲库整理：预览、执行、撤销、空目录。

这里固定几条最容易造成数据损失的行为：
1. 预览绝不移动文件；
2. 目标已存在时拒绝覆盖；
3. 移动音频时 `.lrc` 跟着走；
4. 撤销能回到原路径；
5. 只清理「没有音频」的目录，且含文件的目录是移入回收站而不是抹掉。
"""

from __future__ import annotations

from pathlib import Path

from lyra import organize
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole


def _cfg(tmp_path: Path, lib: Path) -> Config:
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def _flac(path: Path, factory, **entries) -> Path:
    pairs = tuple((key, value) for key, value in entries.items())
    return factory(path, entries=pairs)


def test_target_path_artist_album_track(tmp_path: Path, flac_factory) -> None:
    source = tmp_path / "old" / "x.flac"
    _flac(
        source, flac_factory,
        TITLE="夜曲", ARTIST="周杰伦", ALBUM="十一月的萧邦", TRACKNUMBER="3",
    )
    from lyra.formats import read_all_tags

    tags, error = read_all_tags(source)
    assert error is None
    rel, fields = organize.target_rel_path(tags=tags, source=source)
    assert rel == "周杰伦/十一月的萧邦/03 - 夜曲.flac"
    assert fields["track"] == 3


def test_multi_artist_uses_albumartist_and_semicolon(tmp_path: Path, flac_factory) -> None:
    source = tmp_path / "x.flac"
    _flac(
        source, flac_factory,
        TITLE="Still Breathing", ALBUM="Still Breathing",
        ALBUMARTIST="Faouzia; G.E.M. 鄧紫棋", ARTIST="Faouzia",
        TRACKNUMBER="1",
    )
    from lyra.formats import read_all_tags

    tags, _ = read_all_tags(source)
    rel, _ = organize.target_rel_path(tags=tags, source=source)
    assert rel.startswith("Faouzia; G.E.M. 鄧紫棋/Still Breathing/01 - ")


def test_missing_album_falls_back_under_artist(tmp_path: Path, flac_factory) -> None:
    source = tmp_path / "x.flac"
    _flac(source, flac_factory, TITLE="单曲", ARTIST="某人")
    from lyra.formats import read_all_tags

    tags, _ = read_all_tags(source)
    rel, _ = organize.target_rel_path(tags=tags, source=source)
    assert rel == "某人/Unknown Album/单曲.flac"


def test_preview_execute_and_undo(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    old = lib / "旧目录" / "01 old.flac"
    _flac(
        old, flac_factory,
        TITLE="童年时", ARTIST="张国荣", ALBUM="Salute", TRACKNUMBER="1",
    )
    old.with_suffix(".lrc").write_text("[00:01.00]词\n", encoding="utf-8")
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    root = cfg.roots[0]
    try:
        plan = organize.build_plan(store, cfg, root)
        assert plan["summary"]["total"] == 1
        assert plan["summary"]["move"] == 1
        assert old.is_file(), "预览不能移动文件"
        predicted = [
            item["rel_path"]
            for item in plan["dirs"]
            if item["kind"] == "after_move"
        ]
        assert "旧目录" in predicted, plan["dirs"]
        state = store
        state.save_organize_plan(
            plan_id=plan["plan_id"],
            root_id=state.upsert_root(name="lib", role="library", path=str(lib)),
            subpath="",
            payload=plan,
        )
        row = state.organize_plan(plan["plan_id"])
        assert row is not None
        result = organize.execute_plan(
            state, cfg, root, row, dirs=predicted
        )
        assert result["ok"], result
        target = lib / "张国荣" / "Salute" / "01 - 童年时.flac"
        assert target.is_file()
        assert target.with_suffix(".lrc").is_file()
        assert not old.exists()
        ops = state.organize_ops()
        move_ops = [op for op in ops if op["op_kind"] == "move"]
        assert len(move_ops) == 1
        # 预览里勾选的「移动后变空」目录必须能重建
        assert any(op["op_kind"] == "rmdir" for op in ops)
        undone = organize.undo_organize_ops(
            state, cfg, [int(op["id"]) for op in ops]
        )
        assert undone["ok"], undone
        assert old.is_file()
        assert old.with_suffix(".lrc").is_file()
        assert not target.exists()
    finally:
        store.close()


def test_target_conflict_is_not_overwritten(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    source = lib / "旧" / "x.flac"
    _flac(source, flac_factory, TITLE="歌", ARTIST="甲", ALBUM="专辑", TRACKNUMBER="1")
    occupied = lib / "甲" / "专辑" / "01 - 歌.flac"
    _flac(occupied, flac_factory, TITLE="别人的歌", ARTIST="乙")
    before = occupied.read_bytes()
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        plan = organize.build_plan(store, cfg, cfg.roots[0], subpath="旧")
        assert plan["summary"]["conflict"] == 1
        assert occupied.read_bytes() == before
    finally:
        store.close()


def test_cleanup_only_moves_directories_without_audio(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    (lib / "empty" / "deeper").mkdir(parents=True)
    (lib / "only_cover").mkdir()
    (lib / "only_cover" / "cover.jpg").write_bytes(b"jpg")
    (lib / "has_audio").mkdir()
    _flac(lib / "has_audio" / "x.flac", flac_factory)
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    root = cfg.roots[0]
    try:
        preview = organize.preview_cleanup(root)
        kinds = {item["rel_path"]: item["kind"] for item in preview}
        assert kinds["empty"] == "empty"
        assert kinds["empty/deeper"] == "empty"
        assert kinds["only_cover"] == "no_audio"
        assert "has_audio" not in kinds

        result = organize.cleanup_dirs(
            store, cfg, root, paths=["empty", "empty/deeper", "only_cover"]
        )
        assert result["ok"], result
        assert not (lib / "empty").exists()
        assert not (lib / "only_cover").exists()
        assert (lib / "has_audio" / "x.flac").is_file()
        # 含文件的目录应完整躺在回收站里，可撤销
        trash = cfg.storage.resolved_data_dir / "trash" / result["batch_id"] / "only_cover"
        assert (trash / "cover.jpg").is_file()
        ops = store.organize_ops()
        undone = organize.undo_organize_ops(store, cfg, [int(op["id"]) for op in ops])
        assert undone["ok"], undone
        assert (lib / "only_cover" / "cover.jpg").is_file()
    finally:
        store.close()


def test_multi_include_and_exclude(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    for artist, album, track in (("甲", "专辑A", "1"), ("乙", "专辑B", "2"), ("丙", "专辑C", "3")):
        _flac(
            lib / artist / "旧名.flac",
            flac_factory,
            TITLE=f"{artist} 的歌", ARTIST=artist, ALBUM=album, TRACKNUMBER=track,
        )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        plan = organize.build_plan(
            store, cfg, cfg.roots[0], subpaths=["甲", "乙", "丙"], exclude=["乙"]
        )
        assert plan["summary"]["total"] == 2
        olds = sorted(item["old_rel_path"] for item in plan["executable"])
        assert olds == ["丙/旧名.flac", "甲/旧名.flac"]
        assert plan["subpaths"] == ["甲", "乙", "丙"]
        assert plan["exclude"] == ["乙"]
    finally:
        store.close()


def test_cleanup_scope_respects_exclude(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    (lib / "A" / "empty").mkdir(parents=True)
    (lib / "B" / "empty").mkdir(parents=True)
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        items = organize.preview_cleanup(
            cfg.roots[0], subpaths=["A", "B"], exclude=["B"]
        )
        paths = [item["rel_path"] for item in items]
        assert "A/empty" in paths
        assert "B/empty" not in paths
    finally:
        store.close()
