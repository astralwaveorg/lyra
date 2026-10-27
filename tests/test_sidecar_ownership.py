"""sidecar 归属：同名不同号的三首歌不能互相抢 .lrc。

## 实际踩过的坑

`The Beatles/As Nature Intended` 里有三首同名的 `I've Got a Feeling`
（04 / 07 / 14）。`normalize_stem` 会把编号整个去掉，于是
`04 I've Got a Feeling` 与 `14 I've Got a Feeling` 归一成同一个主体：

  1. 14 号一度把 04 号的 .lrc 当成自己的（编号写法差异那一档配对）；
  2. 写入歌词时，sidecar 目标取自那次观测 → **歌词被写进了 04 号的文件**；
  3. 14 号自己的 .lrc 从来没被创建，界面一直显示「缺 .lrc」；
  4. 而且**不报错**——「内容已经相同」被判成了成功。

所以要钉住两件事：
  - 配对时音轨号冲突的候选直接排除（`04 X.lrc` 不配给 `14 X.flac`）；
  - 写入时绝不把歌词落到**同目录另一个音频**的同名 .lrc 上。
"""

from __future__ import annotations

from pathlib import Path

from lyra.audit import pair_sidecars
from lyra.models import SidecarFinding
from lyra.pipeline import _sidecar_target
from lyra.scan import track_number_hint, track_numbers_compatible


def _sidecar(stem: str) -> SidecarFinding:
    return SidecarFinding(exists=True, size=100)


def test_音轨号提示取最后一段数字():
    assert track_number_hint("04 X") == 4
    assert track_number_hint("1-05 X") == 5
    assert track_number_hint("1. X") == 1
    assert track_number_hint("X") is None


def test_音轨号冲突才算不兼容():
    assert track_numbers_compatible("04 X", "04 X")
    assert track_numbers_compatible("1-05 X", "05 X")
    # 一边没有编号就放行
    assert track_numbers_compatible("X", "04 X")
    # 编号不同就是不同曲目
    assert not track_numbers_compatible("04 X", "14 X")


def test_同名不同号的三首歌不互抢歌词(tmp_path: Path):
    folder = tmp_path / "album"
    folder.mkdir()
    a04 = folder / "04 I’ve Got a Feeling.flac"
    a07 = folder / "07 I’ve Got a Feeling.flac"
    a14 = folder / "14 I’ve Got a Feeling.flac"
    for p in (a04, a07, a14):
        p.write_bytes(b"x")
    lrc04 = folder / "04 I’ve Got a Feeling.lrc"
    lrc07 = folder / "07 I’ve Got a Feeling.lrc"
    lrc04.write_text("[00:01.00]a", encoding="utf-8")
    lrc07.write_text("[00:01.00]b", encoding="utf-8")
    findings = {lrc04: _sidecar(lrc04.stem), lrc07: _sidecar(lrc07.stem)}

    pairing = pair_sidecars([a04, a07, a14], findings)

    assert pairing[a04][0] == lrc04
    assert pairing[a07][0] == lrc07
    # 14 号没有自己的 lrc，就**不要**拿 04 号的来顶替
    assert pairing[a14][0] is None


def test_编号写法差异仍然能配上(tmp_path: Path):
    """`1. 漂向北方.lrc` ↔ `01 漂向北方.flac` 必须照旧配上。"""
    folder = tmp_path / "album"
    folder.mkdir()
    audio = folder / "01 漂向北方.flac"
    audio.write_bytes(b"x")
    lrc = folder / "1. 漂向北方.lrc"
    lrc.write_text("[00:01.00]a", encoding="utf-8")

    pairing = pair_sidecars([audio], {lrc: _sidecar(lrc.stem)})

    assert pairing[audio][0] == lrc


def test_写入目标不会落到别人的同名歌词(tmp_path: Path):
    folder = tmp_path / "album"
    folder.mkdir()
    audio14 = folder / "14 I’ve Got a Feeling.flac"
    audio04 = folder / "04 I’ve Got a Feeling.flac"
    audio14.write_bytes(b"x")
    audio04.write_bytes(b"x")
    lrc04 = folder / "04 I’ve Got a Feeling.lrc"
    lrc04.write_text("[00:01.00]a", encoding="utf-8")

    # 观测到的配对指向了 04 号的 lrc（那本来就是配错的结果）
    target = _sidecar_target(audio14, lrc04)

    assert target == audio14.with_suffix(".lrc")


def test_写入目标沿用异名配对(tmp_path: Path):
    """`09 艺人 - 让你知道.flac` ↔ `让你知道.lrc`：这种异名配对要保留。"""
    folder = tmp_path / "album"
    folder.mkdir()
    audio = folder / "09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac"
    audio.write_bytes(b"x")
    lrc = folder / "让你知道.lrc"
    lrc.write_text("[00:01.00]a", encoding="utf-8")

    assert _sidecar_target(audio, lrc) == lrc


def test_写入目标默认用同名歌词(tmp_path: Path):
    folder = tmp_path / "album"
    folder.mkdir()
    audio = folder / "01 X.flac"
    audio.write_bytes(b"x")

    assert _sidecar_target(audio, None) == folder / "01 X.lrc"
