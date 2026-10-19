"""重复音频检测：只建议、不自动删除。"""

from __future__ import annotations

from pathlib import Path

from lyra import dedupe
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole


def _cfg(tmp_path: Path, lib: Path) -> Config:
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


def test_exact_duplicates_are_grouped_and_suggested(
    tmp_path: Path, flac_factory
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "A").mkdir()
    (lib / "B").mkdir()
    # 音频字节相同、标签不同 → 同一录音的精确副本
    flac_factory(lib / "A" / "song.flac", entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("ALBUM", "专辑")))
    flac_factory(lib / "B" / "song copy.flac", entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("ALBUM", "专辑")))
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        summary = dedupe.scan_duplicates(store, cfg, cfg.roots)
        assert summary["groups"] == 1
        assert summary["duplicates"] == 1
        groups = list(store.iter_dedupe_groups())
        assert len(groups) == 1
        assert groups[0]["kind"] == "exact"
        import json

        items = json.loads(groups[0]["items_json"])
        assert sum(1 for item in items if item["suggested_delete"]) == 1
        # 扫描本身不删除任何文件
        assert (lib / "A" / "song.flac").is_file()
        assert (lib / "B" / "song copy.flac").is_file()
    finally:
        store.close()


def test_delete_requires_keeping_one(tmp_path: Path, flac_factory) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(lib / "a.flac", entries=(("TITLE", "歌"), ("ARTIST", "甲")))
    flac_factory(lib / "b.flac", entries=(("TITLE", "歌"), ("ARTIST", "甲")))
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        group = next(store.iter_dedupe_groups())
        import json

        items = json.loads(group["items_json"])
        rels = [item["rel_path"] for item in items]
        result = dedupe.delete_duplicates(store, cfg, cfg.roots[0], group, rels)
        assert not result["ok"]
        assert result["deleted"] == 0
        assert (lib / rels[0]).is_file()

        victim = next(item["rel_path"] for item in items if item["suggested_delete"])
        result = dedupe.delete_duplicates(store, cfg, cfg.roots[0], group, [victim])
        assert result["deleted"] == 1, result
        assert not (lib / victim).exists()
        assert any((lib / rel).is_file() for rel in rels if rel != victim)
    finally:
        store.close()


def test_same_recording_id_groups_different_audio(
    tmp_path: Path, flac_factory
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(
        lib / "a.flac",
        entries=(
            ("TITLE", "歌"), ("ARTIST", "甲"),
            ("MUSICBRAINZ_TRACKID", "rec-1"),
        ),
        audio=b"\xfe" * 256,
    )
    flac_factory(
        lib / "b.flac",
        entries=(
            ("TITLE", "歌"), ("ARTIST", "甲"),
            ("MUSICBRAINZ_TRACKID", "rec-1"),
        ),
        audio=b"\xfd" * 256,
    )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        groups = list(store.iter_dedupe_groups())
        assert len(groups) == 1
        assert groups[0]["kind"] == "recording"
    finally:
        store.close()


def test_keep_reason_belongs_to_kept_item(tmp_path: Path, flac_factory) -> None:
    """「优先保留」只能出现在建议保留的那一份上，不能挂到待删除副本。"""
    import json

    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(
        lib / "a.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("ALBUM", "专辑")),
    )
    flac_factory(
        lib / "b.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("ALBUM", "专辑")),
    )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        group = next(store.iter_dedupe_groups())
        items = json.loads(group["items_json"])
        keep = next(item for item in items if not item["suggested_delete"])
        delete = next(item for item in items if item["suggested_delete"])
        assert keep["keep_reason"]
        assert delete["keep_reason"] == ""
    finally:
        store.close()


def _groups_by_kind(store):
    import json

    out = {}
    for row in store.iter_dedupe_groups():
        out.setdefault(str(row["kind"]), []).append(
            (row, json.loads(row["items_json"]))
        )
    return out


def test_exact_and_recording_are_separate_categories(
    tmp_path: Path, flac_factory
) -> None:
    """精确副本与同一录音不能混成一组，否则按分类删除会带走别的分类。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(
        lib / "a.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "b.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "c.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
        audio=b"\xfc" * 256,
    )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        groups = _groups_by_kind(store)
        assert len(groups.get("exact", [])) == 1
        assert len(groups.get("recording", [])) == 1

        _, exact_items = groups["exact"][0]
        exact_paths = {item["rel_path"] for item in exact_items}
        assert exact_paths == {"a.flac", "b.flac"}

        _, recording_items = groups["recording"][0]
        deletable = [i for i in recording_items if i["suggested_delete"]]
        assert [i["rel_path"] for i in deletable] == ["c.flac"]
        references = [i for i in recording_items if i.get("reference")]
        assert len(references) == 1
        assert references[0]["rel_path"] in exact_paths
        assert references[0]["suggested_delete"] is False
    finally:
        store.close()


def test_bulk_delete_by_kind_leaves_other_categories(
    tmp_path: Path, flac_factory
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(
        lib / "a.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "b.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "c.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
        audio=b"\xfc" * 256,
    )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        groups = _groups_by_kind(store)
        exact_row, exact_items = groups["exact"][0]
        victim = next(i["rel_path"] for i in exact_items if i["suggested_delete"])
        recording_before = list(groups["recording"][0][1])
        result = dedupe.delete_duplicates_selected(
            store, cfg, {int(exact_row["id"]): [victim]}
        )
        assert result["ok"], result
        assert result["deleted"] == 1
        remaining = _groups_by_kind(store)
        assert "exact" not in remaining
        assert "recording" in remaining
        recording_after = remaining["recording"][0][1]
        assert {
            item["rel_path"] for item in recording_after
        } == {item["rel_path"] for item in recording_before}
    finally:
        store.close()


def test_bulk_delete_rejects_reference_and_keep(
    tmp_path: Path, flac_factory
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    flac_factory(
        lib / "a.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "b.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
    )
    flac_factory(
        lib / "c.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "甲"), ("MUSICBRAINZ_TRACKID", "rec-1")),
        audio=b"\xfc" * 256,
    )
    cfg = _cfg(tmp_path, lib)
    store = open_store(tmp_path / "data")
    try:
        dedupe.scan_duplicates(store, cfg, cfg.roots)
        recording_row, recording_items = _groups_by_kind(store)["recording"][0]
        reference = next(i["rel_path"] for i in recording_items if i.get("reference"))
        result = dedupe.delete_duplicates_selected(
            store, cfg, {int(recording_row["id"]): [reference]}
        )
        assert not result["ok"]
        assert result["deleted"] == 0
        assert (lib / reference).is_file()
    finally:
        store.close()


def test_similar_duration_span_is_not_chained() -> None:
    """1/3/5 秒不能靠相邻差被串成一组；整组跨度必须 <= 2 秒。"""
    from lyra.dedupe import _build_groups

    entries = []
    for index, duration in enumerate((1.0, 3.0, 5.0)):
        entries.append(
            {
                "rel_path": f"{index}.flac",
                "size": 100,
                "mtime": 0.0,
                "format": "flac",
                "audio_hash": f"hash-{index}",
                "duration": duration,
                "title": "歌",
                "artist": "甲",
                "album": "",
                "recording_id": "",
                "has_lrc": False,
                "tag_count": 2,
            }
        )
    groups = _build_groups(entries)
    assert groups
    durations = sorted(
        entries[index]["duration"]
        for group in groups
        for index in group["members"]
    )
    assert durations == [1.0, 3.0]
