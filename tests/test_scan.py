"""递归发现的测试。

为什么测这些：

扫描器决定「哪些文件会被处理」，它漏掉或多认一个文件，后果是双向的：

- **漏掉**（符号链接、跨挂载点、被忽略规则误伤）→ 曲目永远不会被整理。
- **多认**（把 `._*` 资源叉、`.DS_Store`、未完成的 `*.part`、以及**本服务自己
  产生的事务临时文件**当音乐）→ 会去解析垃圾、甚至把自己的中间产物当新曲目，
  形成自我循环。

`normalize_stem` 则是「同一首歌因命名不一致而产生重复歌词文件」的检测基础。
它**只用于检测与报告，绝不用于改名**——这条边界必须守住。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lyra.config import RootConfig
from lyra.scan import (
    IGNORE_DIR_NAMES,
    IGNORE_FILE_PATTERNS,
    is_ignored,
    iter_audio_files,
    iter_sidecar_files,
    normalize_stem,
    sidecar_path_for,
)


def _root(path: Path) -> RootConfig:
    return RootConfig(name="t", path=str(path))


# ---------------------------------------------------------------------------
# normalize_stem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("01 X", "x"),
        ("1. X", "x"),
        ("01 漂向北方", "漂向北方"),
        ("1. 漂向北方", "漂向北方"),
        # 繁简归一 + 去掉分隔符：`鄧` 折成 `邓`，`&`/空格/连字符全部丢弃
        ("09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道", "汪苏泷&gem邓紫棋让你知道"),
        ("01 Sacrifice", "sacrifice"),
        ("X", "x"),
        ("  01   X   ", "x"),
    ],
)
def test_规范化文件名主体(stem: str, expected: str):
    """曲库实际 `01 漂向北方.lrc` 与 `1. 漂向北方.lrc` 并存，必须归一到同一主体。"""
    assert normalize_stem(stem) == expected


def test_规范化大小写不敏感():
    """大小写不同的同名文件必须归一，否则会漏检重复歌词。"""
    assert normalize_stem("01 Song") == normalize_stem("1. song")


def test_碟号音轨号形式也应归一到纯标题():
    """真实库存在 `1-05 X` / `1. X` / `01 X` 三种写法。

    只剥一层编号会让 `1-05 X` 变成 `05 x`，导致 `1-05 歌曲.flac` 与
    `01 歌曲.lrc` 无法归一，sidecar 被误报为孤儿，进而可能再生成一个
    重复的 lrc。曲库整张专辑都使用 `1-NN` 命名，影响面很大。
    """
    assert normalize_stem("1-05 X") == "x"
    assert normalize_stem("1-02 我不懂爱") == "我不懂爱"
    assert normalize_stem("01 X") == "x"
    assert normalize_stem("1. X") == "x"
    assert normalize_stem("1-05-02 X") == "x"
    # 全是数字时不能把主体清空，否则不同曲目会被错误归为一组
    assert normalize_stem("01") == "01"


# ---------------------------------------------------------------------------
# 忽略规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".DS_Store",
        "._resourcefork.flac",
        "Thumbs.db",
        "desktop.ini",
        "song.flac.part",
        "song.flac.tmp",
        "song.flac.temp",
        "song.flac.partial",
        "song.flac.crdownload",
        "song.flac.download",
        "song.flac.!qb",
        "01 song.flac.lyric-txn-abc123.tmp",  # 本服务自己的事务临时文件
        ".lyric-txn-abc123",
        "song.flac~",
    ],
)
def test_应忽略的文件名(name: str):
    assert is_ignored(name) is True


@pytest.mark.parametrize(
    "name",
    ["01 song.flac", "01 song.lrc", "cover.jpg", "album.nfo", "list.m3u8", "song.mp3"],
)
def test_不应忽略的正常文件名(name: str):
    """反向用例：忽略规则必须足够窄，否则会把正常音乐一起跳过。"""
    assert is_ignored(name) is False


def test_忽略规则常量覆盖关键项():
    """这些是实际遇到过的坑，不能从规则表里被删掉。"""
    assert ".DS_Store" in IGNORE_FILE_PATTERNS
    assert any(p == "._*" for p in IGNORE_FILE_PATTERNS)
    assert any("lyric-txn" in p for p in IGNORE_FILE_PATTERNS)
    for directory in (".git", "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found"):
        assert directory in IGNORE_DIR_NAMES


def test_配置追加的忽略模式生效(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "01 a.flac").write_bytes(b"x")
    (root / "02 b.flac").write_bytes(b"x")
    cfg = RootConfig(name="t", path=str(root), ignore_globs=["02 *.flac"])
    assert [p.name for p in iter_audio_files(root, cfg)] == ["01 a.flac"]


def test_配置追加的忽略目录生效(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "keep").mkdir(parents=True)
    (root / "skipme").mkdir(parents=True)
    (root / "keep" / "a.flac").write_bytes(b"x")
    (root / "skipme" / "b.flac").write_bytes(b"x")
    cfg = RootConfig(name="t", path=str(root), ignore_dirs=["skipme"])
    assert [p.name for p in iter_audio_files(root, cfg)] == ["a.flac"]


# ---------------------------------------------------------------------------
# 遍历行为
# ---------------------------------------------------------------------------


def test_递归发现音频并跳过忽略文件(tmp_path: Path):
    """曲库里有 38 个 .DS_Store；它们绝不能被当成资源处理。"""
    root = tmp_path / "lib"
    (root / "artist" / "album").mkdir(parents=True)
    (root / "artist" / "album" / "01 a.flac").write_bytes(b"x")
    (root / "artist" / "album" / "02 b.mp3").write_bytes(b"x")
    (root / "artist" / "album" / "cover.jpg").write_bytes(b"x")
    (root / "artist" / ".DS_Store").write_bytes(b"x")
    (root / "artist" / "._01 a.flac").write_bytes(b"x")
    (root / "artist" / "album" / "03 c.flac.part").write_bytes(b"x")
    (root / ".hidden.flac").write_bytes(b"x")  # 隐藏文件本身不跳过，只有匹配规则的才跳

    found = sorted(p.name for p in iter_audio_files(root, _root(root)))
    assert found == [".hidden.flac", "01 a.flac", "02 b.mp3"]


def test_跳过忽略目录(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "@eaDir").mkdir(parents=True)
    (root / "@eaDir" / "thumb.flac").write_bytes(b"x")
    (root / "01 real.flac").write_bytes(b"x")
    found = sorted(p.name for p in iter_audio_files(root, _root(root)))
    assert found == ["01 real.flac"]


def test_不跟随符号链接文件(tmp_path: Path):
    """设计约束：默认不跟随符号链接。

    跟随会带来「同一文件被处理两次」和「链接指向根外」两个问题。
    """
    root = tmp_path / "lib"
    root.mkdir()
    real = tmp_path / "outside.flac"
    real.write_bytes(b"x")
    link = root / "link.flac"
    os.symlink(real, link)

    assert [p.name for p in iter_audio_files(root, _root(root))] == []


def test_不跟随符号链接目录(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "01 a.flac").write_bytes(b"x")
    os.symlink(outside, root / "linkdir")

    assert [p.name for p in iter_audio_files(root, _root(root))] == []


def test_不跨越不同设备(tmp_path: Path, monkeypatch, fake_scandir_classes):
    """设计约束：不遍历其他挂载点。

    真实的挂载需要 root 权限，这里用替身构造「子目录位于另一个 st_dev」。
    """
    FakeScandir, FakeEntry = fake_scandir_classes
    from lyra import scan

    root = tmp_path / "lib"
    root.mkdir()
    (root / "keep.flac").write_bytes(b"x")
    other = tmp_path / "other-device"
    other.mkdir()
    (other / "skipped.flac").write_bytes(b"x")

    foreign_dev = root.stat().st_dev + 1
    real_scandir = os.scandir

    def fake_scandir(target):
        target = Path(target)
        if target == root:
            return FakeScandir(
                [
                    FakeEntry("keep.flac", root / "keep.flac"),
                    FakeEntry("other", other, is_dir=True, st_dev=foreign_dev),
                ]
            )
        return real_scandir(target)

    monkeypatch.setattr(scan.os, "scandir", fake_scandir)
    assert [p.name for p in scan.iter_audio_files(root, _root(root))] == ["keep.flac"]


def test_根不存在时返回空而不是抛异常(tmp_path: Path):
    """根暂时不可用（挂载故障/误配置）不能让整次扫描崩掉。"""
    missing = tmp_path / "not-there"
    assert list(iter_audio_files(missing, _root(missing))) == []


def test_事务临时文件不会被当成音频(tmp_path: Path):
    """本服务自己写 sidecar 时会生成 `.x.lrc.lyric-txn-<id>.tmp`。

    如果它没被忽略，服务会把自己产生的中间产物当成新资源，
    形成「处理 → 产生临时文件 → 再处理」的自我循环。
    """
    root = tmp_path / "lib"
    root.mkdir()
    (root / "01 a.flac").write_bytes(b"x")
    (root / "01 a.lrc").write_bytes(b"[00:01.00]A")
    (root / ".01 a.lrc.lyric-txn-deadbeef.tmp").write_bytes(b"[00:01.00]A")
    (root / ".01 a.flac.lyric-txn-deadbeef.tmp").write_bytes(b"x")

    assert [p.name for p in iter_audio_files(root, _root(root))] == ["01 a.flac"]
    assert [p.name for p in iter_sidecar_files(root, _root(root))] == ["01 a.lrc"]


# ---------------------------------------------------------------------------
# sidecar 路径约定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "audio,expected",
    [
        ("01 a.flac", "01 a.lrc"),
        ("01 a.mp3", "01 a.lrc"),
        ("song.FLAC", "song.lrc"),
        ("a.b.flac", "a.b.lrc"),  # 只替换最后一个扩展名
    ],
)
def test_sidecar路径约定(audio: str, expected: str):
    """约定：去掉音频最后一个扩展名后加 .lrc（设计约束）。"""
    assert sidecar_path_for(Path(audio)) == Path(expected)


def test_同stem的flac与mp3共享同一个lrc路径():
    """同目录同名 FLAC/MP3 会共用 `.lrc`；这是已知冲突点，需在审计里报出。"""
    assert sidecar_path_for(Path("a/01 song.flac")) == sidecar_path_for(Path("a/01 song.mp3"))


def test_列出sidecar文件(tmp_path: Path):
    root = tmp_path / "lib"
    (root / "album").mkdir(parents=True)
    (root / "album" / "01 a.flac").write_bytes(b"x")
    (root / "album" / "01 a.lrc").write_text("[00:01.00]A", encoding="utf-8")
    (root / "album" / "1. a.LRC").write_text("[00:01.00]A", encoding="utf-8")
    found = sorted(p.name for p in iter_sidecar_files(root, _root(root)))
    assert found == ["01 a.lrc", "1. a.LRC"]


@pytest.mark.parametrize("stem,expected", [
    # 繁简差异：下载来的 lrc 常是简体，而曲库文件名是繁体
    ("01 - 张国荣 - 童年时", "张国荣童年时"),
    ("01 童年時", "童年时"),
    # 「艺人 - 曲名」与纯曲名
    ("齐秦-借根烟", "齐秦借根烟"),
    ("06 齊秦 - 借根菸", "齐秦借根烟"),   # 菸 也折成 烟
])
def test_简繁与艺人前缀也要归一到可比形式(stem: str, expected: str):
    """实际：整张《Salute》的 9 份 lrc 因为繁简不同被误报为孤儿。

    名字是 `01 - 张国荣 - 童年时.lrc`，而音频是 `01 童年時.flac`；
    旧实现只去掉编号，字面不同就配不上，于是「同一首歌的两份歌词」
    被当成「不属于任何曲目」。
    """
    assert normalize_stem(stem) == expected


def test_简繁差异的_lrc_与音频能配成一对():
    """这是上面那条归一化的**目的**：配得上，而不是归一到某个字符串。"""
    from lyra.scan import normalize_stem

    lrc = normalize_stem("01 - 张国荣 - 童年时")
    audio = normalize_stem("01 童年時")
    assert audio in lrc, f"{audio!r} 应包含在 {lrc!r} 里（包含关系即配对依据）"

    lrc2 = normalize_stem("齐秦-借根烟")
    audio2 = normalize_stem("06 齊秦 - 借根菸")
    assert lrc2 == audio2
