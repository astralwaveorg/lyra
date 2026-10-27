"""LRC 解析、分类与规范化的测试。

为什么测这些：

歌词分类是**准确性的第一道关**，也是整个初版里最容易悄悄出错的地方。
这里锁死的行为全部对应真实遇到过的坑：

- **只有元信息标签（`[ti:]`/`[ar:]`）的"歌词文件"必须判为 absent。**
  目标曲库里有 178 个 0 字节 `.lrc`，另有大量只有头部信息的文件。
  如果 `[ti:track 9]` 被当成歌词正文，这些文件会被判为"已有歌词"而永不被填充。
- **占位文案（"纯音乐，请欣赏"）不能当作可用歌词**，更不能被判为 synced 后写进文件。
- **单行歌词不能直接判为无歌词**，只能标记待复核（设计约束）。
- **规范化必须幂等**，否则写入比较会反复认为"内容变了"，产生无意义的重复写入。
- **offset 必须落到显式时间戳上并去掉**，否则播放器会二次应用导致整体偏移。
"""

from __future__ import annotations

import pytest

from lyra.lrc import (
    decode_lyrics_bytes,
    is_placeholder_only,
    normalize_lrc,
    parse_lrc,
)
from lyra.models import LyricsState

# ---------------------------------------------------------------------------
# 基本分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \r\n"])
def test_空白内容判为absent(text: str):
    """0 字节与纯空白必须等价于「没有歌词」，否则 178 个空文件无法被填充。"""
    parsed = parse_lrc(text)
    assert parsed.state is LyricsState.ABSENT
    assert parsed.line_count == 0


def test_纯文本判为plain_text():
    parsed = parse_lrc("第一行\n第二行\n第三行")
    assert parsed.state is LyricsState.PLAIN_TEXT
    assert parsed.line_count == 3
    assert parsed.timestamp_count == 0


def test_标准时间轴判为synced():
    parsed = parse_lrc("[00:04.28]第一行\n[00:06.20]第二行")
    assert parsed.state is LyricsState.SYNCED
    assert parsed.timestamp_count == 2
    assert parsed.line_count == 2


def test_毫秒用冒号分隔也判为synced():
    """`[mm:ss:xx]` 是常见的另一种写法；漏认会把有效歌词误判为纯文本。"""
    parsed = parse_lrc("[00:04:28]第一行\n[00:06:20]第二行")
    assert parsed.state is LyricsState.SYNCED
    assert parsed.timestamp_count == 2


@pytest.mark.parametrize(
    "text",
    [
        "[ti:track 9]\n[ar:汪苏泷]\n[al:明日世界ACT I]",  # 只有元信息
        "[offset:0]",  # 只有 offset
        "[by:]\n[ti:]\n[ar:]",  # 元信息全为空
    ],
)
def test_只有元信息标签必须判为absent(text: str):
    """这是修过的 bug，务必锁死。

    元信息标签被当成歌词正文时，「只有头部信息的文件」会被误判为 plain_text，
    进而被当成"已有歌词"跳过刮削——曲库里大量文件正是这种形态。
    """
    parsed = parse_lrc(text)
    assert parsed.state is LyricsState.ABSENT, f"元信息被当成了歌词正文: {parsed}"
    assert parsed.line_count == 0


def test_元信息被正确解析进tags():
    """判为 absent 的同时，元信息本身仍要能被读到（供报告与复核使用）。"""
    parsed = parse_lrc("[ti:某歌]\n[ar:某歌手]\n[00:01.00]正文")
    assert parsed.tags.get("ti") == "某歌"
    assert parsed.tags.get("ar") == "某歌手"
    assert parsed.state is LyricsState.SYNCED


# ---------------------------------------------------------------------------
# 逐字（增强型）LRC
# ---------------------------------------------------------------------------


def test_逐字标签被记录且状态为synced():
    """标准增强型 LRC：行标签 + 逐字标签共存。"""
    parsed = parse_lrc("[00:04.28]<00:04.28>第一<00:05.00>行\n[00:06.20]<00:06.20>第二行")
    assert parsed.state is LyricsState.SYNCED
    assert parsed.has_word_level is True
    assert parsed.timestamp_count == 2


def test_逐字标签在规范化后被剥离为行级():
    """逐字源必须降级为行级 LRC——播放器兼容性优先（设计约束）。"""
    normalized = normalize_lrc("[00:04.28]<00:04.28>第一<00:05.00>行\n[00:06.20]第二行")
    assert "<" not in normalized and ">" not in normalized
    assert normalized == "[00:04.28]第一行\n[00:06.20]第二行\n"


def test_只有逐字标签也应判为synced():
    """逐字标签也是时间轴。

    早期实现只认 `[mm:ss]`，只有 `<mm:ss.xx>` 的增强型 LRC 会被判为
    plain_text 并在规范化时丢掉全部时间轴——而 plain_text 正是
    「清理无时间轴歌词」的输入，会进一步触发错误的清理决策。
    """
    src = "<00:04.28>第一<00:05.00>行\n<00:06.20>第二行"
    parsed = parse_lrc(src)
    assert parsed.state is LyricsState.SYNCED
    assert parsed.has_word_level is True
    assert parsed.timestamp_count >= 1

    normalized = normalize_lrc(src)
    assert parse_lrc(normalized).state is LyricsState.SYNCED, "规范化不得丢掉时间轴"
    assert "[00:04" in normalized


# ---------------------------------------------------------------------------
# 可疑歌词
# ---------------------------------------------------------------------------


def test_占位文案单独一种状态且标记原因():
    """占位文案绝不能当作可用歌词，更不能写进文件。

    但它也**不是异常**：「此歌曲为没有填词的纯音乐」准确描述了这首歌
    的状态。混进「时间轴异常」会让几十首纯器乐在健康页上挂红灯。
    """
    parsed = parse_lrc("纯音乐，请欣赏")
    assert parsed.state is LyricsState.PLACEHOLDER
    assert "placeholder_only" in parsed.problems


def test_带时间轴的占位文案同样单独一种状态():
    """「有时间轴」不等于「有歌词」——有些源会给占位文案套上时间戳。"""
    parsed = parse_lrc("[00:00.00]纯音乐，请欣赏")
    assert parsed.state is LyricsState.PLACEHOLDER
    assert "placeholder_only" in parsed.problems


def test_全零时间戳判为suspicious():
    """全部时间戳都是 0 说明时间轴是假的，不能当作 synced 直接写入。"""
    parsed = parse_lrc("[00:00.00]第一行\n[00:00.00]第二行")
    assert parsed.state is LyricsState.SYNCED_SUSPICIOUS
    assert "all_timestamps_zero" in parsed.problems


def test_时间戳越界判为suspicious():
    """末尾时间戳远超音频时长 → 很可能是别的版本/别的歌，必须复核。"""
    parsed = parse_lrc("[00:04.28]第一行\n[09:00.00]第二行", duration_seconds=60.0)
    assert parsed.state is LyricsState.SYNCED_SUSPICIOUS
    assert any(p.startswith("timestamp_beyond_duration") for p in parsed.problems)


def test_时长充裕时不报越界():
    """反向用例：证明越界判定不是「总是报错」。"""
    parsed = parse_lrc("[00:04.28]第一行\n[00:30.00]第二行", duration_seconds=60.0)
    assert parsed.state is LyricsState.SYNCED
    assert not any(p.startswith("timestamp_beyond_duration") for p in parsed.problems)


def test_单行歌词只标记待复核而不是判为无歌词():
    """短曲/只有一句人声是合法情况，不能直接判为「没有歌词」（设计约束）。"""
    parsed = parse_lrc("[00:04.28]只有一行")
    assert parsed.state is LyricsState.SYNCED, "单行不应被降级为 plain_text 或无歌词"
    assert "low_text_line_count" in parsed.problems


# ---------------------------------------------------------------------------
# offset
# ---------------------------------------------------------------------------


def test_offset被解析():
    parsed = parse_lrc("[offset:-500]\n[00:10.00]第一行\n[00:12.00]第二行")
    assert parsed.offset_ms == -500
    assert parsed.state is LyricsState.SYNCED


def test_offset落到显式时间戳上并被移除():
    """offset 必须消解成显式时间戳，否则播放器会二次应用造成整体偏移。"""
    normalized = normalize_lrc("[offset:-500]\n[00:10.00]A\n[00:12.00]B")
    assert "offset" not in normalized
    # 10.00s - 0.5s = 9.50s
    assert normalized == "[00:09.50]A\n[00:11.50]B\n"


def test_offset不会产生负时间戳():
    """负偏移 + 极小时间戳时不能写出负时间，那会让播放器行为未定义。"""
    normalized = normalize_lrc("[offset:-9999]\n[00:00.50]A")
    assert "-" not in normalized.split("]")[0], normalized
    assert normalized.startswith("[00:00.00]")


def test_无效offset被记录为问题():
    parsed = parse_lrc("[offset:abc]\n[00:10.00]A")
    assert any(p.startswith("invalid_offset") for p in parsed.problems)


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[offset:-500]\n[00:10.00]A\n[00:12.00]B",
        "<00:04.28>第一<00:05.00>行\n<00:06.20>第二行",
        "[00:04:28]A\n[00:06:20]B",
        "[00:04.283]A\n[00:06.201]B",
        "第一行\n第二行",
        "[00:01.00]A\n[00:02.00]B\n",
        "[ti:x]\n[00:01.00]A",
    ],
)
def test_规范化幂等(text: str):
    """幂等是写入幂等的前提：不幂等会导致每次扫描都认为内容变了而反复写。"""
    once = normalize_lrc(text)
    twice = normalize_lrc(once)
    assert once == twice, f"规范化不幂等: {once!r} -> {twice!r}"


def test_规范化统一为LF且末尾单个换行():
    """落盘格式必须固定：UTF-8 无 BOM、LF、末尾单换行。"""
    normalized = normalize_lrc("[00:01.00]A\r\n[00:02.00]B")
    assert "\r" not in normalized
    assert normalized.endswith("\n")
    assert not normalized.endswith("\n\n")


def test_同一行的多个时间戳展开为多行():
    """LRC 标准语义：一行多个时间戳表示同一句歌词在多个时间点出现。"""
    normalized = normalize_lrc("[00:01.00][00:05.00]重复句")
    assert normalized == "[00:01.00]重复句\n[00:05.00]重复句\n"


def test_亚厘秒差异在规范化后收敛():
    """`[00:04.283]` 与 `[00:04.28]` 规范化后应一致，避免无意义的重复写入。"""
    assert normalize_lrc("[00:04.283]A") == normalize_lrc("[00:04.28]A")


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


def test_解码UTF8无BOM():
    text, bom = decode_lyrics_bytes("中文歌词第一行".encode())
    assert text == "中文歌词第一行"
    assert bom is False


def test_解码UTF8带BOM并报告BOM标志():
    """BOM 标志用于报告「带 BOM 的 sidecar」——某些播放器会显示乱码。"""
    text, bom = decode_lyrics_bytes("\ufeff中文歌词第一行".encode("utf-8"))
    assert text == "中文歌词第一行"
    assert bom is True  # 解码后 BOM 已被剥离，但仍需报告


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-16-le"])
def test_解码UTF16(encoding: str):
    payload = "中文歌词第一行"
    if encoding == "utf-16":
        raw = payload.encode("utf-16")
    elif encoding == "utf-16-be":
        raw = b"\xfe\xff" + payload.encode("utf-16-be")
    else:
        raw = b"\xff\xfe" + payload.encode("utf-16-le")
    text, _ = decode_lyrics_bytes(raw)
    assert text == payload


def test_解码GB18030():
    """真实曲库存在 GBK/GB18030 歌词，只试 UTF-8 会直接丢歌词。"""
    text, bom = decode_lyrics_bytes("中文歌词第一行".encode("gb18030"))
    assert text == "中文歌词第一行"
    assert bom is False


def test_解码空字节():
    text, bom = decode_lyrics_bytes(b"")
    assert text == ""
    assert bom is False


# ---------------------------------------------------------------------------
# 占位判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["纯音乐，请欣赏", "纯音乐,请欣赏", "此歌曲为没有填词的纯音乐", "暂无歌词", "Instrumental"],
)
def test_占位判定识别常见文案(line: str):
    assert is_placeholder_only([line]) is True


def test_占位判定对真实歌词返回False():
    """真实歌词绝不能被误判为占位文案——那会导致整首歌被放弃。"""
    real = [
        "当一艘船沉入海底",
        "当一个人成了谜",
        "你不知道 他们为何离去",
        "那声再见竟是他最后一句",
    ]
    assert is_placeholder_only(real) is False


def test_占位判定要求全部行都是占位():
    """只要有一行真实歌词，就不算占位。"""
    assert is_placeholder_only(["纯音乐，请欣赏", "真的歌词"]) is False


def test_空输入不算占位():
    assert is_placeholder_only([]) is False
    assert is_placeholder_only(["", "   "]) is False


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------


def test_内容指纹对不同格式的等价输入一致():
    """指纹用于判断「内嵌与 sidecar 是否一致」，必须只看规范内容。"""
    from lyra.lrc import content_hash

    a = content_hash("[00:04.28]A\n[00:06.20]B")
    b = content_hash("[00:04.280]A\n[00:06.200]B")
    assert a == b


def test_内容指纹对不同内容不同():
    from lyra.lrc import content_hash

    assert content_hash("[00:01.00]A") != content_hash("[00:01.00]B")


def test_GBK_歌词不会被_UTF16_误读():
    """UTF-16 几乎能"成功"解码任意偶数长度字节串，包括 GBK 中文。

    它排在 gb18030 前面时，GBK 歌词会变成满屏私用区字符——不抛异常、
    静默乱码。实际两份 30 行的同一首歌歌词因此被判成「共有行 0」。
    """
    from lyra.lrc import decode_lyrics_bytes

    raw = "[00:01.00]童年时 我与你家乡中相见天未亮\n".encode("gbk")
    text, _bom = decode_lyrics_bytes(raw)
    assert "童年时" in text, text
    assert "\ufffd" not in text


def test_带_BOM_的_UTF16_仍然能读():
    from lyra.lrc import decode_lyrics_bytes

    raw = "[00:01.00]测试歌词\n".encode("utf-16")
    text, _bom = decode_lyrics_bytes(raw)
    assert "测试歌词" in text
