"""重复文件的自动清理：只处理「完全一样」的，其余仍交人工。

## 判定口径

自动清理要求**四项全同**：音频哈希、时长、标题、艺人、专辑。
其中专辑必须相同是红线——同一录音出现在不同专辑（精选集 vs 原专辑、
限定版 vs 普通版）是**不同发行**，自动删掉会丢内容。

生产实测的四组同录音：
- `exile`（Taylor Swift ft. Bon Iver vs Taylor Swift）→ 艺人不同，不动
- `Something`（`1/` 精选集 vs `Abbey Road/`）→ 专辑不同，不动
- `Journey`（`Over The Rainbow` vs `Over the Rainbow`）→ 专辑名大小写
  不同但规范化后相同、音频哈希不同（不同转码）→ 不动
- `Never Forget You`（限定版 vs 普通版）→ 专辑不同，不动

只有「同一份音频被放进两个目录」才会被自动清理。
"""

from __future__ import annotations

from pathlib import Path

from lyra.dedupe import _auto_same, clean_exact_duplicates


def _entry(rel: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "rel_path": rel,
        "audio_hash": "hash-abc",
        "duration": 180.0,
        "title": "歌",
        "artist": "人",
        "album": "专辑",
        "format": "flac",
        "size": 1000,
        "has_lrc": False,
        "tag_count": 3,
        "reference": False,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def test_完全相同判为可清理():
    a = _entry("a/歌.flac")
    b = _entry("b/歌.flac")
    assert _auto_same(a, b)


def test_专辑不同不清理():
    a = _entry("a/歌.flac", album="精选集")
    b = _entry("b/歌.flac", album="原专辑")
    assert not _auto_same(a, b), "同录音不同专辑是不同发行，不能自动删"


def test_艺人不同不清理():
    a = _entry("a/歌.flac", artist="A")
    b = _entry("b/歌.flac", artist="B")
    assert not _auto_same(a, b)


def test_标题不同不清理():
    a = _entry("a/歌.flac", title="歌")
    b = _entry("b/歌.flac", title="另一首")
    assert not _auto_same(a, b)


def test_音频哈希不同不清理():
    """同一首歌的两次转码：哈希不同——不是「完全一样」，留着。"""
    a = _entry("a/歌.flac", audio_hash="h1")
    b = _entry("b/歌.flac", audio_hash="h2")
    assert not _auto_same(a, b)


def test_时长不同不清理():
    a = _entry("a/歌.flac", duration=180.0)
    b = _entry("b/歌.flac", duration=190.0)
    assert not _auto_same(a, b)


def test_专辑名大小写差异算同一张专辑():
    """`Over The Rainbow` 与 `Over the Rainbow` 是同一张专辑（大小写差异）。"""
    a = _entry("a/歌.flac", album="Over The Rainbow")
    b = _entry("b/歌.flac", album="Over the Rainbow")
    assert _auto_same(a, b)


def test_无哈希不清理():
    a = _entry("a/歌.flac", audio_hash="")
    b = _entry("b/歌.flac", audio_hash="")
    assert not _auto_same(a, b), "没有音频哈希就没有「完全一样」的依据"


# ---------------------------------------------------------------------------
# 端到端：保留最优的一份，其余进回收站
# ---------------------------------------------------------------------------


def test_自动清理保留最优其余进回收站(tmp_path: Path, monkeypatch):
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store

    lib = tmp_path / "lib"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir(parents=True)
    good = lib / "a" / "歌.flac"
    bad = lib / "b" / "歌.flac"
    good.write_bytes(b"x" * 10)
    bad.write_bytes(b"x" * 10)

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    # 两个「完全一样」的文件（音频哈希相同、标签相同）
    items = [
        _entry("a/歌.flac", size=10),
        _entry("b/歌.flac", size=10),
    ]
    store.replace_dedupe_groups(root_id, [{
        "group_key": "k1", "kind": "exact",
        "keep_rel_path": "a/歌.flac", "keep_reason": "FLAC 无损，优先保留",
        "items": items,
    }])
    result = clean_exact_duplicates(store, cfg, cfg.roots)
    assert result["deleted"] == 1, f"应自动清理一个副本：{result}"
    assert result["ok"]
    # 保留的一份还在磁盘上，被清理的进回收站
    assert good.is_file()
    assert not bad.is_file()
    # 回收站里有它，且可撤销（记录了 organize_op）
    ops = store.conn.execute(
        "SELECT op_kind, trash_rel_path FROM organize_ops WHERE old_rel_path=?",
        ("b/歌.flac",),
    ).fetchone()
    assert ops is not None and ops["op_kind"] == "delete"
    assert ops["trash_rel_path"]
    store.close()


def test_不同专辑的重复不会被自动清理(tmp_path: Path):
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store

    lib = tmp_path / "lib"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir(parents=True)
    (lib / "a" / "歌.flac").write_bytes(b"x" * 10)
    (lib / "b" / "歌.flac").write_bytes(b"x" * 10)

    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    store.replace_dedupe_groups(root_id, [{
        "group_key": "k2", "kind": "recording",
        "keep_rel_path": "a/歌.flac", "keep_reason": "",
        "items": [
            _entry("a/歌.flac", album="精选集"),
            _entry("b/歌.flac", album="原专辑"),
        ],
    }])
    result = clean_exact_duplicates(store, cfg, cfg.roots)
    assert result["deleted"] == 0, "不同专辑不应自动清理"
    assert (lib / "b" / "歌.flac").is_file()
    store.close()


def test_干跑不删文件(tmp_path: Path):
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store

    lib = tmp_path / "lib"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir(parents=True)
    (lib / "a" / "歌.flac").write_bytes(b"x" * 10)
    (lib / "b" / "歌.flac").write_bytes(b"x" * 10)
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    store.replace_dedupe_groups(root_id, [{
        "group_key": "k3", "kind": "exact",
        "keep_rel_path": "a/歌.flac", "keep_reason": "",
        "items": [_entry("a/歌.flac"), _entry("b/歌.flac")],
    }])
    result = clean_exact_duplicates(store, cfg, cfg.roots, dry_run=True)
    assert result["deleted"] == 0
    assert result["groups"] == 1, "干跑也要报出待处理数量"
    assert (lib / "b" / "歌.flac").is_file(), "干跑不碰文件"
    store.close()


# ---------------------------------------------------------------------------
# 第二层：同一首歌的重复副本（路径规范化后相同）
# ---------------------------------------------------------------------------


def test_同一位置的重复下载判为可清理():
    """`潘朵拉/口袋的天空.flac` 与 `潘朵拉/08 - 口袋的天空.flac`。

    只差一个音轨号前缀——同一位置留下了两份，保留一份即可。
    """
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "潘朵拉/口袋的天空.flac", audio_hash="h1", duration=261.77,
        title="口袋的天空", artist="張韶涵", album="潘朵拉",
    )
    b = _entry(
        "潘朵拉/08 - 口袋的天空.flac", audio_hash="h2", duration=263.77,
        title="口袋的天空", artist="張韶涵", album="潘朵拉",
    )
    assert _auto_same_song(a, b)


def test_目录大小写差异的重复判为可清理():
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "張韶涵/Over the Rainbow/遺失的美好.flac", audio_hash="h1",
        duration=260.98, title="遺失的美好", artist="張韶涵", album="Over the Rainbow",
    )
    b = _entry(
        "張韶涵/Over The Rainbow/11 - 遺失的美好.flac", audio_hash="h2",
        duration=261.93, title="遺失的美好", artist="張韶涵", album="Over the Rainbow",
    )
    assert _auto_same_song(a, b)


def test_不同专辑的同名曲不判为重复():
    """`The Beatles/1/24 - Something` 与 `Abbey Road/Something`：不同发行。"""
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "The Beatles/1/24 - Something.flac", audio_hash="h1", duration=181.2,
        title="Something", artist="The Beatles", album="1",
    )
    b = _entry(
        "The Beatles/Abbey Road/Something.flac", audio_hash="h2", duration=182.0,
        title="Something", artist="The Beatles", album="Abbey Road",
    )
    assert not _auto_same_song(a, b)


def test_署名差异不判为重复():
    """`Taylor Swift ft. Bon Iver` 与 `Taylor Swift`：不同署名。"""
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "Taylor Swift/folklore/04 - exile.flac", audio_hash="h1", duration=285.64,
        title="exile", artist="Taylor Swift ft. Bon Iver", album="folklore",
    )
    b = _entry(
        "Taylor Swift/folklore/06 - exile.flac", audio_hash="h2", duration=285.63,
        title="exile", artist="Taylor Swift", album="folklore",
    )
    assert not _auto_same_song(a, b)


def test_时长差过大不判为重复():
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "A/歌.flac", audio_hash="h1", duration=100.0, album="专辑",
    )
    b = _entry(
        "A/01 - 歌.flac", audio_hash="h2", duration=130.0, album="专辑",
    )
    assert not _auto_same_song(a, b)


def test_路径不同不判为重复():
    """同专辑同歌但在不同目录（不是同一位置的两份）。"""
    from lyra.dedupe import _auto_same_song

    a = _entry(
        "張韶涵/潘朵拉/口袋的天空.flac", audio_hash="h1", duration=261.0,
        title="口袋的天空", artist="張韶涵", album="潘朵拉",
    )
    b = _entry(
        "張韶涵/别的专辑/口袋的天空.flac", audio_hash="h2", duration=261.0,
        title="口袋的天空", artist="張韶涵", album="潘朵拉",
    )
    assert not _auto_same_song(a, b)
