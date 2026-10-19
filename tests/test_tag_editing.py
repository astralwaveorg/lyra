"""任意标签改写的测试。

## 核心不变量

这套写入器的根本承诺是「**音频数据逐字节不变**」。现在允许改任意标签，
但那条承诺**不能**放宽——改标签绝不能顺带改写音频。
本文件用真实文件逐条验证：

1. 改标签后音频数据哈希完全不变；
2. 未涉及的字段按原字节、原顺序保留（不能重排、不能丢失重复项）；
3. 被替换的字段保留原位置，而不是被挪到末尾；
4. 封面（PICTURE 块）不受影响；
5. 多值字段（如两个 ARTISTS）能正确写入。
"""

from __future__ import annotations

from pathlib import Path

from lyra.flacmeta import (
    BlockType,
    VorbisComments,
    flac_audio_hash,
    flac_parse_file,
    flac_rewrite_tags,
)


def test_音频数据逐字节不变(tmp_path, flac_factory):
    """最重要的那条：改标签绝对不能碰音频。"""
    source = flac_factory(tmp_path / "a.flac")
    target = tmp_path / "b.flac"
    before = flac_audio_hash(source)

    flac_rewrite_tags(source, target, tags={"TITLE": ["新标题"], "GENRE": ["Rock"]})

    assert flac_audio_hash(target) == before, "音频数据被改动了——这是不可接受的"


def test_未涉及的字段原样保留(tmp_path, flac_factory):
    source = flac_factory(tmp_path / "a.flac")
    target = tmp_path / "b.flac"
    flac_rewrite_tags(source, target, tags={"GENRE": ["Jazz"]})

    original = flac_parse_file(source).comments()
    updated = flac_parse_file(target).comments()
    assert original is not None and updated is not None

    untouched_orig = [(n, r) for n, r in original.entries if n != b"GENRE"]
    untouched_new = [(n, r) for n, r in updated.entries if n != b"GENRE"]
    assert untouched_new == untouched_orig, "未涉及的字段必须逐字节、按原顺序保留"


def test_替换保留原位置而不是挪到末尾():
    """同一专辑内字段顺序一致；把改过的字段挪到末尾会让那个文件显得异样。"""
    comments = VorbisComments(
        vendor=b"v",
        entries=(
            (b"TITLE", b"TITLE=old"),
            (b"ARTIST", b"ARTIST=A"),
            (b"ALBUM", b"ALBUM=X"),
        ),
    )
    updated = comments.with_tags({"TITLE": ["new"]})
    assert [n for n, _ in updated.entries] == [b"TITLE", b"ARTIST", b"ALBUM"]
    assert updated.get("TITLE") == [b"new"]


def test_新增字段追加到末尾():
    comments = VorbisComments(vendor=b"v", entries=((b"TITLE", b"TITLE=t"),))
    updated = comments.with_tags({"MOOD": ["calm"]})
    assert [n for n, _ in updated.entries] == [b"TITLE", b"MOOD"]


def test_删除字段():
    comments = VorbisComments(
        vendor=b"v",
        entries=((b"TITLE", b"TITLE=t"), (b"LYRICS", b"LYRICS=x"), (b"ALBUM", b"ALBUM=a")),
    )
    updated = comments.with_tags(remove=["LYRICS"])
    assert [n for n, _ in updated.entries] == [b"TITLE", b"ALBUM"]


def test_多值字段():
    comments = VorbisComments(vendor=b"v", entries=())
    updated = comments.with_tags({"ARTISTS": ["艺人甲", "艺人乙"]})
    assert updated.get("ARTISTS") == ["艺人甲".encode(), "艺人乙".encode()]


def test_重复项被替换而不是叠加():
    """原始文件里同一字段可能有重复项；替换必须整体取代，不能越改越多。"""
    comments = VorbisComments(
        vendor=b"v",
        entries=((b"ARTISTS", b"ARTISTS=A"), (b"ARTISTS", b"ARTISTS=B")),
    )
    updated = comments.with_tags({"ARTISTS": ["C"]})
    assert updated.get("ARTISTS") == [b"C"]
    assert len([n for n, _ in updated.entries if n == b"ARTISTS"]) == 1


def test_封面块不受影响(tmp_path, flac_factory):
    source = flac_factory(tmp_path / "a.flac")
    target = tmp_path / "b.flac"
    flac_rewrite_tags(source, target, tags={"TITLE": ["x"]})

    before = flac_parse_file(source).block(BlockType.PICTURE)
    after = flac_parse_file(target).block(BlockType.PICTURE)
    assert before == after, "PICTURE 块必须原字节保留"


def test_歌词字段也能通过通用接口改写(tmp_path, flac_factory):
    """通用接口不该给歌词开特例——它只是众多字段之一。"""
    source = flac_factory(tmp_path / "a.flac")
    target = tmp_path / "b.flac"
    flac_rewrite_tags(source, target, tags={"LYRICS": ["[00:01.00]词"]})
    comments = flac_parse_file(target).comments()
    assert comments is not None and comments.get("LYRICS") == ["[00:01.00]词".encode()]


# ---------------------------------------------------------------------------
# 写入层：原子替换 + 校验
# ---------------------------------------------------------------------------


def _plan(path: Path, **kw):
    from lyra.models import MediaFormat
    from lyra.writer import TagEditPlan, audio_hash_for

    return TagEditPlan(
        audio_path=path,
        media_format=kw.pop("media_format", MediaFormat.FLAC),
        original_audio_hash=audio_hash_for(path, MediaFormat.FLAC) or "",
        **kw,
    )


def test_写入层改写后音频仍逐字节不变(tmp_path, flac_factory):
    from lyra.writer import apply_tag_edit

    path = flac_factory(tmp_path / "a.flac")
    before = flac_audio_hash(path)

    outcome = apply_tag_edit(_plan(path, tags={"TITLE": ["改过的标题"]}))

    assert outcome.ok, outcome.error
    assert flac_audio_hash(path) == before
    comments = flac_parse_file(path).comments()
    assert comments is not None and comments.get("TITLE") == ["改过的标题".encode()]


def test_写入层拒绝不支持的格式(tmp_path, flac_factory):
    """无法识别的格式必须**明确拒绝**而不是尽力而为——
    写坏标签比不写更糟。FLAC / MP3 / OGG 现在各有独立的字节级写入器。"""
    from lyra.models import MediaFormat
    from lyra.writer import apply_tag_edit

    path = flac_factory(tmp_path / "a.flac")
    outcome = apply_tag_edit(
        _plan(path, tags={"TITLE": ["x"]}, media_format=MediaFormat.UNSUPPORTED)
    )
    assert not outcome.ok
    assert "暂不支持" in (outcome.error or "")


def test_文件被外部改动后拒绝写入(tmp_path, flac_factory):
    """并发保护：拿到的哈希与当前不符时不能写，否则会覆盖别人的改动。"""
    from lyra.writer import TagEditPlan, apply_tag_edit

    path = flac_factory(tmp_path / "a.flac")
    plan = TagEditPlan(
        audio_path=path,
        media_format=__import__("lyra.models", fromlist=["MediaFormat"]).MediaFormat.FLAC,
        tags={"TITLE": ["x"]},
        original_audio_hash="deadbeef" * 8,
    )
    outcome = apply_tag_edit(plan)
    assert not outcome.ok
    assert "已被改动" in (outcome.error or "")


def test_写入失败不留临时文件(tmp_path, flac_factory, monkeypatch):
    """任何失败都不能在音乐目录里留下半成品。"""
    from lyra import flacmeta
    from lyra.writer import apply_tag_edit

    path = flac_factory(tmp_path / "a.flac")

    def boom(*_a, **_kw):
        raise RuntimeError("模拟写盘失败")

    monkeypatch.setattr(flacmeta, "flac_rewrite_tags", boom)
    outcome = apply_tag_edit(_plan(path, tags={"TITLE": ["x"]}))

    assert not outcome.ok
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".lyra-tags-")]
    assert leftovers == [], f"留下了临时文件: {leftovers}"


# ---------------------------------------------------------------------------
# 全量标签读取
# ---------------------------------------------------------------------------


def test_能看到白名单之外的标签(tmp_path, flac_factory):
    """标签编辑器必须能看到**全部**字段。

    实际真实文件的 33 个标签里，匹配白名单只覆盖 15 个；
    `QMQUALITY`、`ORIGINALDATE`、`RELEASETYPE`、`CATALOGNUMBER` 等等
    在白名单之外。若编辑器只读白名单，用户会遇到
    「界面上没有这个标签，所以我改不了」。
    """
    from lyra.formats import read_all_tags

    path = flac_factory(tmp_path / "a.flac")
    flac_rewrite_tags(path, tmp_path / "b.flac", tags={"QMQUALITY": ["Premium-Master"]})
    (tmp_path / "b.flac").replace(path)

    tags, error = read_all_tags(path)
    assert error is None
    assert tags.get("QMQUALITY") == ["Premium-Master"]


def test_全量标签保留多值(tmp_path, flac_factory):
    from lyra.formats import read_all_tags

    path = flac_factory(tmp_path / "a.flac")
    flac_rewrite_tags(path, tmp_path / "b.flac", tags={"ARTISTS": ["甲", "乙"]})
    (tmp_path / "b.flac").replace(path)

    tags, _ = read_all_tags(path)
    assert tags["ARTISTS"] == ["甲", "乙"]


def test_非FLAC明确报错而不是返回空(tmp_path):
    """返回空字典会被误读成「这个文件没有标签」，必须给出明确说明。"""
    from lyra.formats import read_all_tags

    ogg = tmp_path / "a.ogg"
    ogg.write_bytes(b"OggS" + b"\x00" * 64)
    tags, error = read_all_tags(ogg)
    assert tags == {}
    assert error, "必须给出错误说明，不能静默返回空"


# ---------------------------------------------------------------------------
# 通用改名
# ---------------------------------------------------------------------------


def test_改名会同步改名配对的歌词(tmp_path, flac_factory):
    """不改 .lrc 的话歌词会变成孤儿，播放器再也读不到。"""
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store
    from lyra.models import RootRole
    from lyra.rename import apply_general_rename

    lib = tmp_path / "lib"
    lib.mkdir()
    audio = flac_factory(lib / "old.flac")
    (lib / "old.lrc").write_text("[00:01.00]词\\n", encoding="utf-8")

    cfg = Config(roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
                 storage=StorageConfig(data_dir=str(tmp_path / "data")))
    store = open_store(tmp_path / "data")
    try:
        result = apply_general_rename(store, cfg.roots[0], source=audio, new_name="new.flac")
    finally:
        store.close()

    assert result.ok, result.error
    assert (lib / "new.flac").is_file()
    assert (lib / "new.lrc").is_file(), "歌词必须跟着改名"
    assert not (lib / "old.lrc").exists()


def test_改名拒绝覆盖已存在的文件(tmp_path, flac_factory):
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store
    from lyra.models import RootRole
    from lyra.rename import apply_general_rename

    lib = tmp_path / "lib"
    lib.mkdir()
    a = flac_factory(lib / "a.flac")
    flac_factory(lib / "b.flac")

    cfg = Config(roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
                 storage=StorageConfig(data_dir=str(tmp_path / "data")))
    store = open_store(tmp_path / "data")
    try:
        result = apply_general_rename(store, cfg.roots[0], source=a, new_name="b.flac")
    finally:
        store.close()
    assert not result.ok
    assert "拒绝覆盖" in (result.error or "")


def test_改名会提前警告播放列表引用(tmp_path, flac_factory):
    """改完才说「播放列表失效了」是没用的，必须改之前就告知。"""
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store
    from lyra.models import RootRole
    from lyra.rename import apply_general_rename

    lib = tmp_path / "lib"
    lib.mkdir()
    audio = flac_factory(lib / "old.flac")
    (lib / "list.m3u8").write_text("#EXTM3U\\nold.flac\\n", encoding="utf-8")

    cfg = Config(roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
                 storage=StorageConfig(data_dir=str(tmp_path / "data")))
    store = open_store(tmp_path / "data")
    try:
        result = apply_general_rename(store, cfg.roots[0], source=audio, new_name="new.flac")
    finally:
        store.close()
    assert result.ok
    assert any("播放列表" in w for w in result.warnings)
