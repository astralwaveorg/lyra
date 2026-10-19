"""判定曲目属于「华语圈」还是「外语圈」，用于决定歌词来源的查询顺序。

## 为什么需要它

默认按语种采用不同的来源顺序：

    华语：QQ → 网易云 → LRCLIB → AI 兜底
    外语：LRCLIB → 网易云 → QQ → AI 兜底

理由是各源在不同语种上的覆盖差异极大：QQ 的华语曲库与同步歌词最全，
而 LRCLIB 对西方流行/摇滚的覆盖明显更好。用一套固定顺序必然在另一半曲目上吃亏。

## 判定依据及其取舍

用标题与艺人里**文字系统**（script）的构成来判断，而不是：

- 语言检测库：要引入重依赖，且对「泡沫」「天下」这种两三个字的短标题准确率很差；
- 专辑/厂牌/年份：与语种没有稳定关系；
- 逐字翻译后再判断：多一次网络往返，且失败时会退化成猜测。

Unicode 的文字系统区间是确定的，短标题也够用，且完全离线、零依赖、可测试。

## 日韩为什么归到「华语圈」优先

严格说日语、韩语属于「外语」，但实际 QQ 音乐对 J-Pop / K-Pop 的曲库与
歌词覆盖同样很强（国内平台对亚洲曲目的授权较全），而 LRCLIB 在这两块明显偏弱。
因此默认把汉字/假名/谚文都归入「CJK 圈」，走 QQ 优先。

这是一个**可配置的取舍**，不是硬编码的真理：
`providers.cjk_uses_zh_priority = false` 可把日韩单独划回外语圈。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence

__all__ = [
    "Script",
    "detect_script",
    "priority_group",
    "PRIORITY_GROUPS",
    "ordered_providers",
]

#: 优先级分组名。配置里的 `providers.priority.<组名>` 用它作键。
PRIORITY_GROUPS = ("zh", "foreign")


class Script(str):
    """文字系统。用 str 子类是为了能直接当字典键、也便于打印。"""


_HAN = "HAN"
_KANA = "KANA"
_HANGUL = "HANGUL"
_LATIN = "LATIN"
_OTHER = "OTHER"


def _classify_char(ch: str) -> str:
    """把单个字符归到一个文字系统。"""
    code = ord(ch)
    # 假名：平假名 U+3040–309F、片假名 U+30A0–30FF、半角片假名 U+FF66–FF9D
    if 0x3040 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9D:
        return _KANA
    # 谚文：U+AC00–D7AF 及 Jamo 区
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return _HANGUL
    # 汉字（含扩展 A）。注意：日文里的汉字也会落在这里，所以先判假名再判汉字。
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return _HAN
    if ch.isalpha() and "LATIN" in unicodedata.name(ch, ""):
        return _LATIN
    if ch.isalpha():
        return _OTHER
    return ""  # 标点、数字、空白都忽略


def detect_script(fields: Sequence[str]) -> str:
    """判断一组文本主要使用哪种文字系统。

    返回 `han` / `kana` / `hangul` / `latin` / `other` / `none`。

    规则：假名与谚文**优先于**汉字——因为日文标题常常「汉字+假名」混排，
    若只看汉字占比会把日文误判成中文。
    """
    counts: dict[str, int] = {}
    for text in fields:
        for ch in text or "":
            kind = _classify_char(ch)
            if kind:
                counts[kind] = counts.get(kind, 0) + 1

    if not counts:
        return "none"
    if counts.get(_KANA):
        return "kana"
    if counts.get(_HANGUL):
        return "hangul"
    if counts.get(_HAN):
        return "han"
    if counts.get(_LATIN):
        return "latin"
    return "other"


def priority_group(
    title: str,
    artists: Sequence[str] = (),
    *,
    cjk_uses_zh_priority: bool = True,
) -> str:
    """把曲目归到 `zh` 或 `foreign` 优先级分组。

    **以标题为主判据**，艺人只在标题完全没有字母（纯数字/符号）时兜底。

    为什么不能让艺人参与主要判定：实际 `Still Breathing` 的标题是英文，
    但艺人是 `Faouzia, G.E.M. 鄧紫棋 & ILLENIUM`——含汉字。若两者一起统计，
    这首**英文歌**会被判成华语而走 QQ 优先，与「外语歌用 LRCLIB」的意图相反。
    歌曲的语言由标题体现，艺人的语言是另一回事。
    """
    title_script = detect_script([title])
    if title_script in ("none", "other"):
        # 标题里没有可用字母（例如纯数字、纯符号），才回头看艺人
        title_script = detect_script(list(artists))
    if title_script == "han":
        return "zh"
    if title_script in ("kana", "hangul"):
        return "zh" if cjk_uses_zh_priority else "foreign"
    return "foreign"


def ordered_providers(
    enabled: Sequence[str],
    priority: Mapping[str, Sequence[str]],
    group: str,
) -> list[str]:
    """按分组优先级重排来源列表。

    规则：
      1. 该分组明确列出的来源，按列出顺序排前面；
      2. 其余来源保持 `enabled` 里的相对顺序跟在后面（**不丢弃**——
         用户可能启用了优先级表里没列的来源）；
      3. 结果里不含 `enabled` 之外的来源。
    """
    preferred = [p for p in priority.get(group, ()) if p in enabled]
    rest = [p for p in enabled if p not in preferred]
    return preferred + rest
