"""文件名解析的测试。

为什么测这些：文件名是补歌词/补标签时唯一「总有」的元数据来源，解析错了
上层拿到的就是错的歌名——不会报错，只会安静地匹配到别的歌。每条断言对应
一种真实曲库里出现过的命名习惯：

- 前导编号的三种写法（`01 `、`02.`、`1-05`）在同一张专辑里并存
- 括号里的版本标记、合作艺人、年份必须与标题本身分开
- 目录名是「艺人 - 歌名」组织的库中最可靠的旁证，繁简写法要能对上
- 病态输入（空串、纯符号）绝不抛异常——扫描会碰到各种垃圾文件名
"""

from __future__ import annotations

from lyra.filename_parser import parse_filename

# ---------------------------------------------------------------------------
# 前导编号
# ---------------------------------------------------------------------------


def test_编号加连字符形态剥出音轨号与艺人():
    """`01 - 周杰伦 - 晴天`：编号连同分隔符一起剥掉，剩下「艺人 - 歌名」。"""
    hints = parse_filename("01 - 周杰伦 - 晴天")
    assert hints.track_no == 1
    assert hints.disc_no is None
    assert hints.artists == ("周杰伦",)
    assert hints.title == "晴天"


def test_多段编号取最后一段为音轨号():
    """`1-05` 的音轨号是 5、碟号是 1——只剥一层会剥错，scan 里踩过。"""
    hints = parse_filename("1-05 Love Me Do")
    assert hints.track_no == 5
    assert hints.disc_no == 1
    assert hints.artists == ()
    assert hints.title == "Love Me Do"


def test_点号编号形态():
    hints = parse_filename("02. 潘朵拉")
    assert hints.track_no == 2
    assert hints.title == "潘朵拉"


def test_传入带路径的输入只取最后一段():
    hints = parse_filename("some/dir/01 - A - B")
    assert hints.track_no == 1
    assert hints.artists == ("A",)
    assert hints.title == "B"


# ---------------------------------------------------------------------------
# 括号折叠
# ---------------------------------------------------------------------------


def test_版本括号在后是常规形态():
    """`艺人 - 歌名 (Live)`：默认按「艺人 - 标题」读，版本不留在标题里。"""
    hints = parse_filename("周杰伦 - 晴天 (Live)")
    assert hints.artists == ("周杰伦",)
    assert hints.title == "晴天"
    assert hints.version_tokens == ("live",)


def test_版本括号在前时标题与艺人不互换():
    """`晴天 (Live) - 周杰伦`：版本挂在标题上，形态其实是「标题 - 艺人」。"""
    hints = parse_filename("晴天 (Live) - 周杰伦")
    assert hints.title == "晴天"
    assert hints.artists == ("周杰伦",)
    assert "live" in hints.version_tokens


def test_feat括号提取合作艺人():
    """调用方传不带扩展名的 stem；feat 内容不属于标题。"""
    hints = parse_filename("艺人 - 歌名 (feat. 某人)")
    assert hints.artists == ("艺人",)
    assert hints.title == "歌名"
    assert hints.feat == ("某人",)


def test_ft写法同样识别():
    hints = parse_filename("歌名 [ft. 另一人]")
    assert hints.title == "歌名"
    assert hints.feat == ("另一人",)


def test_feat括号里的多艺人是列表():
    hints = parse_filename("歌名 (feat. A & B)")
    assert hints.feat == ("A", "B")


def test_纯数字括号视为年份丢弃():
    hints = parse_filename("晴天 (2006)")
    assert hints.title == "晴天"
    assert hints.version_tokens == ()


def test_未知括号文本并入标题():
    """既非版本也非 feat 的括号内容是标题的一部分，不能丢。"""
    hints = parse_filename("晴天 (数字修复)")
    assert hints.title == "晴天 数字修复"


# ---------------------------------------------------------------------------
# 主干拆分
# ---------------------------------------------------------------------------


def test_三段拆出专辑():
    hints = parse_filename("A - B - C")
    assert hints.artists == ("A",)
    assert hints.album == "B"
    assert hints.title == "C"
    assert hints.artists_conf == 0.6
    assert hints.title_conf == 0.7


def test_两段默认前段是艺人():
    hints = parse_filename("周杰伦 - 晴天")
    assert hints.artists == ("周杰伦",)
    assert hints.title == "晴天"
    assert hints.artists_conf == 0.6
    assert hints.title_conf == 0.8


def test_破折号分隔同样拆出艺人():
    """中文文件名常用「—」且不带空格，也要能拆。"""
    hints = parse_filename("周杰伦—晴天")
    assert hints.artists == ("周杰伦",)
    assert hints.title == "晴天"


def test_无分隔符时整段当标题():
    hints = parse_filename("Love Me Do")
    assert hints.title == "Love Me Do"
    assert hints.artists == ()
    assert hints.title_conf == 0.6


def test_分隔符过多时不猜艺人():
    """三处以上分隔分不清哪段是艺人，宁要完整标题不要拆错。"""
    hints = parse_filename("A - B - C - D")
    assert hints.artists == ()
    assert hints.title == "A - B - C - D"


def test_连续分隔符不产生空段():
    """`A -  - B` 按两段读，不拆出空专辑。"""
    hints = parse_filename("A -  - B")
    assert hints.artists == ("A",)
    assert hints.title == "B"
    assert hints.album == ""


# ---------------------------------------------------------------------------
# 目录名旁证
# ---------------------------------------------------------------------------


def test_目录名等于艺人名时置信度提升():
    boosted = parse_filename("周杰伦 - 晴天", parent_dir="周杰伦")
    plain = parse_filename("周杰伦 - 晴天")
    assert boosted.artists_conf == 0.9
    assert boosted.title_conf == 0.85
    assert plain.artists_conf == 0.6
    assert plain.title_conf == 0.8


def test_目录名含年份等额外信息时不误判专辑():
    """「张韶涵 - 潘朵拉 (2006)」不等于艺人名，也不能当专辑来源。"""
    hints = parse_filename("01 - 张韶涵 - 潘朵拉", parent_dir="张韶涵 - 潘朵拉 (2006)")
    assert hints.track_no == 1
    assert "张韶涵" in hints.artists
    assert hints.title == "潘朵拉"
    assert hints.album == ""


def test_繁简归一后目录名匹配():
    """标签/目录常繁简混用，相等判断必须走归一化；hints 保留原文。"""
    trad = parse_filename("張韶涵 - 隱形的翅膀", parent_dir="張韶涵")
    simp = parse_filename("張韶涵 - 隱形的翅膀", parent_dir="张韶涵")
    assert trad.artists_conf == 0.9
    assert simp.artists_conf == 0.9
    assert trad.artists == ("張韶涵",)
    assert trad.title == "隱形的翅膀"


# ---------------------------------------------------------------------------
# 兜底容错
# ---------------------------------------------------------------------------


def test_病态输入不抛异常():
    """扫描会碰到各种垃圾文件名，解析必须总能返回。"""
    empty = parse_filename("")
    assert empty.title == ""
    assert empty.title_conf == 0.0
    assert empty.artists == ()

    for stem in ("   ", "---", "（）", " - ", "/", "\\", "（）"):
        hints = parse_filename(stem)
        assert hints.title == ""
        assert hints.artists == ()


def test_纯编号文件名只剥出音轨号():
    only_no = parse_filename("07")
    assert only_no.track_no == 7
    assert only_no.title == ""


def test_原文不被归一化改写():
    """全角、繁体等写法原样保存，归一化只发生在比较时。"""
    hints = parse_filename("ＧＥＭ－隱形的翅膀")
    assert hints.title == "ＧＥＭ－隱形的翅膀"
