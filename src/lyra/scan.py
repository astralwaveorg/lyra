"""递归发现：受管根下的音频与 sidecar 歌词。

必须遵守的边界：
- 默认不跟随符号链接
- 不遍历其他挂载点
- 跳过本服务的事务临时文件与常见下载器临时文件
- 路径分量校验，拒绝越界
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path

from .config import RootConfig
from .formats import AUDIO_EXTENSIONS

__all__ = [
    "IGNORE_DIR_NAMES",
    "IGNORE_FILE_PATTERNS",
    "SIDECAR_SUFFIX",
    "iter_audio_files",
    "iter_sidecar_files",
    "normalize_stem",
    "sidecar_path_for",
    "is_ignored",
]

SIDECAR_SUFFIX = ".lrc"

#: 任意层级都跳过的目录名
IGNORE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git", ".svn", ".hg",
        "@eaDir",            # 群晖缩略图
        "#recycle", "$RECYCLE.BIN", ".Trash", ".Trashes",
        "node_modules", "__pycache__", ".venv",
        "lost+found",
    }
)

#: 跳过的文件名模式
IGNORE_FILE_PATTERNS: tuple[str, ...] = (
    ".DS_Store",
    "._*",                 # macOS 资源叉
    "Thumbs.db",
    "desktop.ini",
    "*.tmp",
    "*.temp",
    "*.part",
    "*.partial",
    "*.crdownload",
    "*.download",
    "*.!qb",               # qBittorrent 未完成
    "*.lyric-txn-*.tmp",   # 本服务事务临时文件
    ".lyric-txn-*",
    "*~",
)


def is_ignored(name: str, *, extra_patterns: tuple[str, ...] = ()) -> bool:
    """文件名是否应跳过。"""
    for pattern in IGNORE_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in extra_patterns)


def _walk(root: Path, cfg: RootConfig) -> Iterator[Path]:
    """遍历受管根，返回其中的文件（不跟随符号链接、不跨挂载点）。"""
    root = Path(root)
    ignore_dirs = IGNORE_DIR_NAMES | set(cfg.ignore_dirs)
    extra = tuple(cfg.ignore_globs)

    try:
        root_dev = root.stat().st_dev
    except OSError:
        return

    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            # 权限/IO 问题不中断整次扫描；上层可通过「根健康」发现异常
            continue

        for entry in entries:
            try:
                if entry.is_symlink():
                    # 默认不跟随符号链接
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in ignore_dirs:
                        continue
                    # 不跨挂载点
                    try:
                        if entry.stat(follow_symlinks=False).st_dev != root_dev:
                            continue
                    except OSError:
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if is_ignored(entry.name, extra_patterns=extra):
                        continue
                    yield Path(entry.path)
            except OSError:
                continue


def iter_audio_files(root: Path, cfg: RootConfig) -> Iterator[Path]:
    """产出受管根下所有候选音频文件（按真实扩展名筛选，格式判定留给 formats）。"""
    for path in _walk(root, cfg):
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            yield path


def iter_sidecar_files(root: Path, cfg: RootConfig) -> Iterator[Path]:
    """产出受管根下所有 .lrc 文件。"""
    for path in _walk(root, cfg):
        if path.suffix.lower() == SIDECAR_SUFFIX:
            yield path


def sidecar_path_for(audio: Path) -> Path:
    """音频文件对应的同名 .lrc 路径。

    约定：去掉音频最后一个扩展名后加 .lrc
    `01 歌曲.flac` → `01 歌曲.lrc`
    """
    return audio.with_suffix(SIDECAR_SUFFIX)


# 前导编号前缀：可能是「音轨号」，也可能是「碟号-音轨号」，甚至多层。
# 实际真实库中 `1-05 X`、`1. X`、`01 X` 三种写法并存，且 `1-NN` 命名
# 在整张专辑里大量出现。只剥一层会让 `1-05 X` 变成 `05 x`，
# 导致同一首歌的 sidecar 配不上、被误报为孤儿，进而产生重复歌词文件。
_LEADING_TRACK_NO = re.compile(
    r"^\s*\d+\s*(?:[-._]\s*\d+\s*)*(?:[-._]\s*)?"
)


#: 匹配时忽略的分隔符。`张国荣 - 童年时` 与 `童年时` 只在「艺人 - 」这一段不同，
#: 保留连字符会让包含关系断掉，于是同一首歌被报成「孤儿 lrc」。
_SEPARATORS = re.compile(r"[\s\-_.,;:!?/\\|·•~()\[\]{}<>]+")


def normalize_stem(stem: str) -> str:
    """规范化文件名主体，用于**配对与重复检测**。

    只用于检测与配对，绝不用于改写文件名。

    要处理的真实差异（都来自曲库）：
      - 编号写法：`1. 漂向北方` / `1-05 X` / `01 X`
      - **简繁差异**：`01 - 张国荣 - 童年时.lrc` ↔ `01 童年時.flac`
      - 艺人前缀：`张国荣 - 童年时` ↔ `童年时`
      - 分隔符：`齐秦-借根烟` ↔ `06 齊秦 - 借根菸`

    只做「去编号」不够：繁简不同字面就不同，包含关系直接断掉，
    实际整张专辑 9 份 lrc 因此被误报为孤儿。
    """
    from .match import normalize_text

    s = stem.strip()
    stripped = _LEADING_TRACK_NO.sub("", s).strip()
    # 整个主体都是数字时不要把结果清空，否则会把不同曲目错误归为一组
    s = stripped or s
    # NFKC + 全角标点 + 去引号 + **繁转简** + casefold
    s = normalize_text(s)
    # 最后去掉所有分隔符：包含关系只看「字」
    return _SEPARATORS.sub("", s)


def track_number_hint(stem: str) -> int | None:
    """文件名开头的**音轨号**（取最后一段数字），没有则返回 None。

    为什么需要它：`normalize_stem` 会把 `04 X` 和 `14 X` 都归一成 `x`，
    于是「同一首歌的不同编号写法」与「两首不同的歌」就分不开了。
    实际后果：`The Beatles/As Nature Intended` 里三首同名的
    `I've Got a Feeling`（04 / 07 / 14）互相抢 sidecar，
    14 号因此把 04 号的 lrc 当成自己的——写入时把歌词写进了别人的文件，
    自己那一份却始终「缺 .lrc」。

    取**最后一段**数字：`1-05` 的音轨号是 5（第一段是碟号）。
    """
    s = stem.strip()
    match = _LEADING_TRACK_NO.match(s)
    if not match:
        return None
    prefix = match.group(0)
    numbers = re.findall(r"\d+", prefix)
    if not numbers:
        return None
    try:
        return int(numbers[-1])
    except ValueError:
        return None


def track_numbers_compatible(left: str, right: str) -> bool:
    """两个文件名的音轨号是否**不冲突**。

    只有两边都带编号、且编号不同才算冲突；有一边没编号就放行
    （`让你知道.lrc` 这种没有编号的 sidecar 无法据此判断）。
    """
    a = track_number_hint(left)
    b = track_number_hint(right)
    if a is None or b is None:
        return True
    return a == b
