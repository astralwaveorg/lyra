"""按语种决定来源优先级的测试。

为什么值得单独测：来源顺序错了**不会报错**，只会让每首歌都去问一个
在该语种上覆盖很差的源，表现为「命中率莫名偏低」——很难从现象定位到原因。
所以把判定规则和排序规则都固定下来。

关键案例来自真实曲库：
  - `Still Breathing` 标题是英文，艺人是 `Faouzia, G.E.M. 鄧紫棋 & ILLENIUM`（含汉字）。
    若用「标题+艺人一起统计」，这首英文歌会被判成华语而走 QQ 优先，
    与「外语歌用 LRCLIB」的意图相反。因此判定必须以标题为主。
"""

from __future__ import annotations

import pytest

from lyra.lang import detect_script, ordered_providers, priority_group


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("泡沫", "han"),
        ("突然之間", "han"),
        ("Still Breathing", "latin"),
        ("残酷な天使のテーゼ", "kana"),
        ("Dynamite", "latin"),
        ("사랑해", "hangul"),
        ("2024", "none"),
    ],
)
def test_文字系统判定(title: str, expected: str):
    assert detect_script([title]) == expected


def test_假名优先于汉字():
    """日文标题常是「汉字+假名」混排，只看汉字占比会把日文误判成中文。"""
    assert detect_script(["残酷な天使のテーゼ"]) == "kana"
    assert detect_script(["東京"]) == "han"  # 纯汉字确实无法区分，归华语是合理近似


@pytest.mark.parametrize(
    ("title", "artists", "expected"),
    [
        ("泡沫", ["G.E.M. 鄧紫棋"], "zh"),
        ("突然之间", ["G.E.M. 鄧紫棋"], "zh"),
        # 真实反例：英文歌 + 中文艺人，必须走外语优先
        ("Still Breathing", ["Faouzia, G.E.M. 鄧紫棋 & ILLENIUM"], "foreign"),
        ("Bohemian Rhapsody", ["Queen"], "foreign"),
        ("Dynamite", ["BTS"], "foreign"),
        # 标题没有可用字母时，回退看艺人
        ("2024", ["G.E.M. 鄧紫棋"], "zh"),
        ("2024", ["Queen"], "foreign"),
    ],
)
def test_分组以标题为主判据(title: str, artists: list[str], expected: str):
    assert priority_group(title, artists) == expected


def test_日韩默认归华语优先级但可配置():
    """实际 QQ 对 J-Pop/K-Pop 覆盖同样强，所以默认并入；但这是可改的取舍。"""
    assert priority_group("残酷な天使のテーゼ", ["高橋洋子"]) == "zh"
    assert (
        priority_group(
            "残酷な天使のテーゼ", ["高橋洋子"], cjk_uses_zh_priority=False
        )
        == "foreign"
    )


def test_华语与外语得到相反的顺序():
    enabled = ["qq", "netease", "lrclib"]
    priority = {"zh": ["qq", "netease", "lrclib"], "foreign": ["lrclib", "netease", "qq"]}
    assert ordered_providers(enabled, priority, "zh") == ["qq", "netease", "lrclib"]
    assert ordered_providers(enabled, priority, "foreign") == ["lrclib", "netease", "qq"]


def test_优先级表里没列的来源不会被丢掉():
    """用户可能启用了优先级表未覆盖的来源，必须保留在末尾而不是静默丢弃。"""
    enabled = ["qq", "netease", "lrclib", "acoustid"]
    priority = {"zh": ["qq"], "foreign": ["lrclib"]}
    assert ordered_providers(enabled, priority, "zh") == [
        "qq", "netease", "lrclib", "acoustid",
    ]


def test_优先级表里未启用的来源不会凭空出现():
    enabled = ["qq", "lrclib"]
    priority = {"zh": ["netease", "qq", "lrclib"], "foreign": ["lrclib"]}
    assert ordered_providers(enabled, priority, "zh") == ["qq", "lrclib"]
