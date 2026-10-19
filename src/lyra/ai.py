"""AI 辅助判定：只回答「这两份歌词是不是同一首」这类语义问题。

## 为什么需要它

Lyra 的匹配全靠可验证的证据（时长、版本、标题、艺人）——这是对的，
但它答不了「两份**看起来都像**的歌词，哪一份属于这首歌」：

    `01 童年時.flac`
      ← `01 童年時.lrc`（从内嵌歌词生成，22 行）
      ← `01 - 张国荣 - 童年时.lrc`（下载来的，18 行、开头的作词/作曲行不同）

两份都是真歌词、都对着同一个音频。要不要清理、留哪一份，取决于**内容**，
而不是文件名或时长。

## 边界

- 模型只给「判断 + 理由 + 置信度」，**动作仍由 Lyra 执行**（移入回收站可还原）；
- 调用失败、返回不是 JSON、置信度不够，一律当作「没有判断」，不改任何文件；
- 一轮最多问 `max_requests_per_run` 次，避免一次点击把上游打爆。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

__all__ = [
    "AIError", "Judgement", "review_duplicate", "extract_json",
    "text_overlap", "lyric_lines", "orphan_coverage",
]


class AIError(Exception):
    """调用或解析失败。调用方应当**当作没有判断**，不做任何动作。"""


@dataclass(slots=True)
class Judgement:
    same_song: bool
    confidence: float
    keep: str
    """`existing` / `orphan` / `either` / `unknown`"""
    reason: str
    model: str
    overlap: float = 0.0
    """两份歌词**去掉时间轴后的正文行**重合比例（0–1）。

    这是确定性计算的，不是模型给的：清理是不可逆的（虽进回收站），
    不能只凭一句"我看像同一首"。
    """
    orphan_covered: float = 0.0
    """孤儿那份的**歌词行**有多大比例能在现有那份里找到（0–1）。

    =1.0 表示它一行歌词都没多出来（只多出作词/作曲/标题这类元信息），
    清掉它不会丢内容；<1.0 说明它有自己的歌词行，得留给人看。
    """

    def to_dict(self) -> dict:
        return {
            "same_song": self.same_song,
            "confidence": self.confidence,
            "keep": self.keep,
            "reason": self.reason,
            "model": self.model,
            "overlap": self.overlap,
            "orphan_covered": round(self.orphan_covered, 3),
        }


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_TIME_TAG = re.compile(r"\[[^\]]*\]")


def text_lines(text: str) -> list[str]:
    """歌词去掉时间轴与空白后的正文行（用于比较"内容是否一样"）。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        body = _TIME_TAG.sub("", line).strip()
        if body:
            # 全角/半角空格、不可见空格都归一，否则「同一句」会被算成不同
            out.append(re.sub(r"\s+", "", body))
    return out


#: 作词/作曲/编曲这类元信息行——它们不同不算"内容不同"。
_META_LINE = re.compile(
    r"^\s*(?:作词|作曲|编曲|填词|谱曲|词|曲|制作人|监制|出品|发行|混音|母带|"
    r"吉他|贝斯|鼓|和声|OP|SP|ti|ar|al|by|offset)\s*[:：]", re.I)


#: 「曲名 - 艺人 (英文名)」这类标题行。生成 .lrc 时写在第一行的就是它，
#: 例如 `童年时 - 张国荣 (Leslie Cheung)`、`借根烟-齐秦(ChyiChin)`。
#: 它只是抬头的两种写法，不是歌词——判断"有没有多出歌词"时要排除。
_TITLE_LINE = re.compile(r"^.{1,40}[-–—]\s*.{1,40}\([A-Za-z][^)]*\)\s*$")


def lyric_lines(text: str) -> list[str]:
    """只保留**歌词行**：去掉时间轴、空白、作词/作曲与标题行。"""
    return [
        line for line in text_lines(text)
        if not _META_LINE.match(line) and not _TITLE_LINE.match(line)
    ]


def orphan_coverage(existing_text: str, orphan_text: str) -> float:
    """孤儿那份的歌词行有多大比例能被现有那份覆盖。

    1.0 = 一行歌词都没多出来 → 清掉它不丢内容。
    """
    existing = set(lyric_lines(existing_text))
    orphan = lyric_lines(orphan_text)
    if not orphan:
        return 1.0
    return sum(1 for line in orphan if line in existing) / len(orphan)


def text_overlap(a: str, b: str) -> float:
    """两份歌词正文的重合比例（以较少的一份为分母）。"""
    la, lb = set(text_lines(a)), set(text_lines(b))
    if not la or not lb:
        return 0.0
    return len(la & lb) / min(len(la), len(lb))

_PROMPT = """你是音乐库整理助手。下面是一个音频文件，以及两份歌词文件。
判断这两份歌词是否属于**同一首歌**，并给出保留建议。

音频信息：
- 标题：{title}
- 艺人：{artist}
- 专辑：{album}
- 时长：{duration}

先看一个**确定性**的对照（由程序统计，不是你的估计）：
去掉时间轴后，两份正文共有 {shared} 行；以较少的一份为分母，重合 {overlap:.0%}。
B 的歌词行有 {covered:.0%} 能在 A 里找到（B 多出 {orphan_extra} 行歌词）。
A 有而 B 没有 {a_only} 行，B 有而 A 没有 {b_only} 行。

【A】现有 .lrc（{a_name}，{a_lines} 行）
{a_text}

【B】另一份 .lrc（{b_name}，{b_lines} 行）
{b_text}

只输出一个 JSON 对象，不要解释、不要代码块：
{{"same_song": true/false,
  "confidence": 0 到 1 的数字,
  "keep": "A" 或 "B" 或 "either",
  "reason": "一句中文理由（30 字以内）"}}

判断要点：
- 同一首歌的两份歌词，正文高度重复（可能只差前奏、作词作曲行、时间轴精度）；
- 不同歌曲即使标题相同，正文也完全不同；
- 若两份正文几乎一致，keep 填 "either"；
- 若 B 多出 A 没有的**歌词行**（B 多出 0 行才是纯重复），
  same_song 仍可填 true，但 keep 填 "either" 且 confidence 不要超过 0.6；
- 拿不准就把 confidence 写低。"""


def extract_json(text: str) -> dict:
    """从模型输出里取出 JSON（容忍 ```json 包裹与前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        raise AIError("模型返回为空")
    match = _JSON_BLOCK.search(raw)
    if match is None:
        raise AIError("模型没有返回 JSON")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise AIError(f"模型返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise AIError("模型返回的不是对象")
    return data


def _clip(text: str, *, max_lines: int = 24, max_chars: int = 1200) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()][:max_lines]
    body = "\n".join(lines)
    return body[:max_chars]


def review_duplicate(
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    audio: dict,
    existing_name: str,
    existing_text: str,
    orphan_name: str,
    orphan_text: str,
    client: httpx.Client | None = None,
) -> Judgement:
    """让模型判断两份歌词是不是同一首，并建议留哪一份。

    任何异常都抛 `AIError`；调用方收到异常必须当作「没有判断」。
    """
    if not api_key.strip():
        raise AIError("未配置 AI 密钥")

    la, lb = text_lines(existing_text), text_lines(orphan_text)
    shared = len(set(la) & set(lb))
    overlap = shared / max(1, min(len(la), len(lb)))
    covered = orphan_coverage(existing_text, orphan_text)
    orphan_extra = sum(1 for line in lyric_lines(orphan_text)
                       if line not in set(lyric_lines(existing_text)))
    prompt = _PROMPT.format(
        shared=shared,
        overlap=overlap,
        covered=covered,
        orphan_extra=orphan_extra,
        a_only=len(set(la) - set(lb)),
        b_only=len(set(lb) - set(la)),
        title=audio.get("title") or "（未知）",
        artist=", ".join(audio.get("artists") or []) or "（未知）",
        album=audio.get("album") or "（未知）",
        duration=f"{audio.get('duration'):.1f}s" if audio.get("duration") else "（未知）",
        a_name=existing_name, a_lines=len(existing_text.splitlines()),
        a_text=_clip(existing_text),
        b_name=orphan_name, b_lines=len(orphan_text.splitlines()),
        b_text=_clip(orphan_text),
    )
    own = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        response = client.post(
            base_url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise AIError(f"AI 请求失败：HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise AIError(f"AI 请求失败：{exc}") from exc
    finally:
        if own:
            client.close()

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("AI 返回结构不符合预期") from exc
    data = extract_json(str(content))

    keep = str(data.get("keep") or "unknown").strip()
    if keep.upper() in ("A", "EXISTING"):
        keep = "existing"
    elif keep.upper() in ("B", "ORPHAN"):
        keep = "orphan"
    elif keep.lower() in ("either", "both"):
        keep = "either"
    else:
        keep = "unknown"
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return Judgement(
        same_song=bool(data.get("same_song")),
        confidence=max(0.0, min(1.0, confidence)),
        keep=keep,
        reason=str(data.get("reason") or "")[:120],
        model=model,
        overlap=overlap,
        orphan_covered=covered,
    )
