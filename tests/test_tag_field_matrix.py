"""逐字段覆盖规则矩阵：每个标签字段的冲突如何处置。

## 设计原则（对应实际曲库的 2074 条冲突字段的分布）

1. **语义相同的值不是冲突**（简繁 `周杰伦`↔`周杰倫`、全半角空格
   `G.E.M.鄧紫棋`↔`G.E.M. 鄧紫棋`、冠词倒置 `Beatles, The`↔`The Beatles`、
   轨号 `01`↔`1`、日期 `2021`↔`2021-09-02`）——对比层直接判 SAME，
   不进人工队列。
2. **本地更具体时保留本地**：专辑的版本括号 `Red (Taylor's Version)`、
   `Midnights [Jade Green Edition]` 是人工整理的版次痕迹，来源的基础名
   并不更准。
3. **日期按谱系判定**：本地年份与来源的 ORIGINALDATE 同谱系时（本地记
   原始发行、来源记版次发行），两者都对。
4. **真冲突保持人工**：轨号/碟号/总数不同（可能是另一版次的曲目表）、
   明显不同的专辑名（坏匹配）、不同年份。
5. **强证据下写规范形**（MBID 精确/核验搜索/可信锚点）：人名类字段的
   写法差异直接以来源规范形落盘，整库归一——用户明确要求这类覆盖
   完全不需要人工。
6. **身份字段永不自动覆盖**：MBID/ISRC/条形码不等价就是另一个录音，
   留人工。

矩阵逐行固定在 `FIELD_MATRIX` 里；新增字段时先在这里加行再实现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.tagscrape import SOURCE_MBID, MatchResult, TrackSnapshot, _auto_resolve_conflicts
from lyra.tagsources import ACTION_CONFLICT, ACTION_CORRECT, ACTION_SAME, compare_fields


def _action(current: dict, suggested: dict) -> str:
    """取「本地字段那一行」的动作：矩阵里每条用例的 current 只有一个字段，
    而返回行会按风险排序（来源独有的 fill 行可能排在前面）。"""
    (key,) = current.keys()
    for row in compare_fields(current, suggested):
        if row["key"] == key:
            return str(row["action"])
    raise AssertionError(f"没有 {key} 行")


FIELD_MATRIX = [
    # ---- 艺人 / 人名：简繁、空格、冠词、扩展 ----
    pytest.param({"ARTIST": ["周杰伦"]}, {"ARTIST": ["周杰倫"]}, "same", id="艺人-简繁"),
    pytest.param({"ARTIST": ["張韶涵"]}, {"ARTIST": ["张韶涵"]}, "same", id="艺人-繁转简"),
    pytest.param(
        {"ARTIST": ["G.E.M.鄧紫棋"]}, {"ARTIST": ["G.E.M. 鄧紫棋"]}, "same", id="艺人-缺空格",
    ),
    pytest.param(
        {"ALBUMARTIST": ["G.E.M.鄧紫棋"]}, {"ALBUMARTIST": ["G.E.M. 鄧紫棋"]}, "same",
        id="专辑艺人-缺空格",
    ),
    pytest.param(
        {"ARTIST": ["Beatles, The"]}, {"ARTIST": ["The Beatles"]}, "same", id="艺人-冠词倒置",
    ),
    pytest.param(
        {"ARTIST": ["G.E.M."]}, {"ARTIST": ["G.E.M. 鄧紫棋"]}, "fill", id="艺人-缺成员=补齐",
    ),
    pytest.param(
        {"COMPOSER": ["G.E.M.邓紫棋"]}, {"COMPOSER": ["G.E.M. 鄧紫棋"]}, "same", id="作曲-简繁空格",
    ),
    pytest.param(
        {"LYRICIST": ["方文山"]}, {"LYRICIST": ["方文山"]}, "same", id="作词-相同",
    ),
    pytest.param(
        {"ARTIST": ["李玟"]}, {"ARTIST": ["CoCo 李玟"]}, "conflict", id="艺人-真不同留人工",
    ),
    # ---- 标题 ----
    pytest.param({"TITLE": ["爱在西元前"]}, {"TITLE": ["愛在西元前"]}, "same", id="标题-简繁"),
    pytest.param(
        {"TITLE": ["一字這種小煩惱"]}, {"TITLE": ["一字这种小烦恼"]}, "same", id="标题-简繁异字",
    ),
    pytest.param(
        {"TITLE": ["爱在西元前 (Live)"]}, {"TITLE": ["愛在西元前"]}, "conflict",
        id="标题-版本标记不同留人工",
    ),
    # ---- 专辑 ----
    pytest.param(
        {"ALBUM": ["G.E.M.鄧紫棋"]}, {"ALBUM": ["G.E.M. 鄧紫棋"]}, "conflict",
        id="专辑-非人名字段空格仍算差异",
    ),
    pytest.param(
        {"ALBUM": ["Red (Taylor’s Version)"]}, {"ALBUM": ["Red"]}, "same",
        id="专辑-本地是具体版次",
    ),
    pytest.param(
        {"ALBUM": ["Midnights [Jade Green Edition]"]}, {"ALBUM": ["Midnights"]}, "same",
        id="专辑-版本方括号",
    ),
    pytest.param(
        {"ALBUM": ["Midnights（3am Edition）"]}, {"ALBUM": ["Midnights"]}, "same",
        id="专辑-全角括号版本",
    ),
    pytest.param(
        {"ALBUM": ["1989"]}, {"ALBUM": ["Taylor Swift Karaoke: 1989"]}, "conflict",
        id="专辑-坏匹配留人工",
    ),
    pytest.param({"ALBUM": ["范特西"]}, {"ALBUM": ["八度空间"]}, "conflict", id="专辑-真不同"),
    pytest.param({"ALBUM": ["Red CD1"]}, {"ALBUM": ["Red"]}, "same", id="专辑-裸碟后缀"),
    pytest.param(
        {"ALBUM": ["最偉大的作品 (Greatest Works of Art)"]}, {"ALBUM": ["最伟大的作品"]}, "same",
        id="专辑-双语括号注释",
    ),
    pytest.param(
        {"ALBUM": ["啓示錄 REVELATION"]}, {"ALBUM": ["REVELATION"]}, "same",
        id="专辑-并排双语名",
    ),
    pytest.param({"ALBUM": ["[unknown]"]}, {"ALBUM": ["11月的蕭邦"]}, "fill", id="专辑-占位值补齐"),
    pytest.param(
        {"ALBUM": ["Love"]}, {"ALBUM": ["Love Me Do"]}, "conflict", id="专辑-短名不是长名的前缀冲突",
    ),
    pytest.param(
        {"ALBUM": ["十一月的萧邦"]}, {"ALBUM": ["11月的蕭邦"]}, "same", id="专辑-中文数字",
    ),
    pytest.param({"DATE": ["2019-12-27"]}, {"DATE": ["2019-12-26"]}, "same", id="日期-差一天"),
    pytest.param({"DATE": ["2010-10-29"]}, {"DATE": ["2010-10-22"]}, "conflict", id="日期-差七天"),
    # ---- 日期 ----
    pytest.param({"DATE": ["2021"]}, {"DATE": ["2021-09-02"]}, "same", id="日期-精确到日"),
    pytest.param(
        {"DATE": ["2012"]},
        {"DATE": ["2021-09-02"], "ORIGINALDATE": ["2012-05-07"]},
        "same", id="日期-原始发行同谱系",
    ),
    pytest.param(
        {"DATE": ["2022"]}, {"DATE": ["2024"], "ORIGINALDATE": ["2024-04-19"]}, "conflict",
        id="日期-不同年份留人工",
    ),
    # ---- 结构数值 ----
    pytest.param({"TRACKNUMBER": ["01"]}, {"TRACKNUMBER": ["1"]}, "same", id="轨号-前导零"),
    pytest.param({"TRACKNUMBER": ["1"]}, {"TRACKNUMBER": ["3"]}, "conflict", id="轨号-不同留人工"),
    pytest.param({"TOTALTRACKS": ["16"]}, {"TOTALTRACKS": ["13"]}, "conflict", id="总数-不同留人工"),
    # ---- 身份 ----
    pytest.param(
        {"ISRC": ["TWA410500102"]}, {"ISRC": ["TWA410500103"]}, "conflict", id="ISRC-不同留人工",
    ),
    pytest.param(
        {"MUSICBRAINZ_TRACKID": ["aaa"]}, {"MUSICBRAINZ_TRACKID": ["bbb"]}, "conflict",
        id="MBID-不同留人工",
    ),
    # ---- 流派：本地优先（补充信息，不是身份）----
    pytest.param({"GENRE": ["Pop"]}, {"GENRE": ["Pop / Blue-eyed Soul"]}, "same", id="流派-本地优先"),
]


@pytest.mark.parametrize(
    "current,suggested,expected", FIELD_MATRIX, ids=[p.id for p in FIELD_MATRIX]
)
def test_字段矩阵(current, suggested, expected):
    assert _action(current, suggested) == expected


def test_日期谱系只看DATE行():
    """谱系判定作用于 DATE 行本身；ORIGINALDATE 本地空时照常补齐。"""
    rows = compare_fields(
        {"DATE": ["2012"]},
        {"DATE": ["2021-09-02"], "ORIGINALDATE": ["2012-05-07"]},
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["DATE"]["action"] == "same", "本地记原始发行，来源记版次发行，两者都对"
    assert by_key["ORIGINALDATE"]["action"] == "fill", "本地没有 ORIGINALDATE，顺手补上"


def test_总数别名互认不重复补():
    """本地 TOTALTRACKS=16、来源 TRACKTOTAL=16：同一个值换个名字，
    不产生「缺 TRACKTOTAL 要补」的假待办。"""
    rows = compare_fields({"TOTALTRACKS": ["16"]}, {"TRACKTOTAL": ["16"]})
    assert [(r["key"], r["action"]) for r in rows] == [("TRACKTOTAL", "same")]


def test_流派本地为空时才补():
    rows = compare_fields({"GENRE": []}, {"GENRE": ["Pop"]})
    assert rows[0]["action"] == "fill"
    # 多值只取第一个，避免把来源的长流派链抄进库
    assert rows[0]["suggested"] == ["Pop"]


# ---------------------------------------------------------------------------
# 强证据下的写法归一：同一个署名/值，写法取来源规范形（无需人工）
# ---------------------------------------------------------------------------


def _snap(tags: dict[str, list[str]]) -> TrackSnapshot:
    return TrackSnapshot(
        location_id=1, rel_path="a.flac", path=Path("a.flac"),
        tags=tags, duration=300.0, media_format="flac", writable=True,
        dir=".", file_size=1, file_mtime=1,
        title=(tags.get("TITLE") or [""])[0],
        artists=tuple(tags.get("ARTISTS") or tags.get("ARTIST") or ()),
        album=(tags.get("ALBUM") or [""])[0],
        albumartist=(tags.get("ALBUMARTIST") or [""])[0],
    )


def _resolve_strong(local: dict[str, list[str]], remote: dict[str, list[str]]) -> dict:
    """MBID 精确匹配（最强证据）下的自动消解结果。"""
    snap = _snap(local)
    rows = _auto_resolve_conflicts(
        compare_fields(local, remote),
        MatchResult(source=SOURCE_MBID, confidence=1.0),
        snap,
    )
    return {row["key"]: row for row in rows}


STRONG_EVIDENCE_MATRIX = [
    # 字段, 本地, 来源, 期望
    ("ALBUMARTIST", ["G.E.M.鄧紫棋"], ["G.E.M. 鄧紫棋"], ACTION_CORRECT),
    ("ARTIST", ["周杰伦"], ["周杰倫"], ACTION_CORRECT),
    ("ARTIST", ["Beatles, The"], ["The Beatles"], ACTION_CORRECT),
    ("COMPOSER", ["G.E.M.邓紫棋"], ["G.E.M. 鄧紫棋"], ACTION_CORRECT),
    ("TITLE", ["爱在西元前"], ["愛在西元前"], ACTION_CORRECT),
    # 字面相同：不该产生无谓写入
    ("LYRICIST", ["林夕"], ["林夕"], ACTION_SAME),
    # 日期精度升级不自动写（避免无谓改文件；谱系判定已保证不算冲突）
    ("DATE", ["2012"], ["2012-05-07"], ACTION_SAME),
    # 真不同：不自动
    ("ARTIST", ["李玟"], ["CoCo 李玟"], None),
]


@pytest.mark.parametrize("key,local_val,remote_val,expected", STRONG_EVIDENCE_MATRIX)
def test_强证据下写法归一(key, local_val, remote_val, expected):
    local_tags = {key: local_val}
    remote_tags = {key: remote_val}
    rows = _resolve_strong(local_tags, remote_tags)
    if expected is None:
        assert rows[key]["action"] == ACTION_CONFLICT
    else:
        assert rows[key]["action"] == expected


def test_强证据下ALBUM版本括号保留本地():
    """本地 `Red (Taylor's Version)` 在 MBID 精确匹配下也不被基础名覆盖：
    版次名是人工整理的痕迹，且 MB 命中的可能正是基础发行。"""
    rows = _resolve_strong(
        {"ALBUM": ["Red (Taylor’s Version)"]}, {"ALBUM": ["Red"]},
    )
    assert rows["ALBUM"]["action"] == ACTION_SAME, "版本括号已在对比层判同，不写"
