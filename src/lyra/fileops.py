"""文件搬运的共用工具。

回收站是**移动**而不是删除：这些都是用户的音乐，误删无法挽回；同一卷内
rename 是瞬时的。web 任务、监控进程、AI 清理都走这一份实现，否则「还原」
会依赖各自私有的目录结构。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config, RootConfig

__all__ = ["move_to_trash", "trash_root"]


def trash_root(cfg: Config) -> Path:
    return cfg.storage.resolved_data_dir / "trash"


def move_to_trash(cfg: Config, root: RootConfig, path: Path, batch: str) -> str:
    """把文件移进回收站，**保留它在曲库里的相对路径**。

    为什么必须保留目录结构：早先只存 `时间戳__文件名`，把原目录丢了，
    于是「还原」根本无从谈起——同名文件还可能来自不同专辑。
    现在结构是 `trash/<批次>/<原相对路径>`，还原就是原样搬回去。
    """
    trash = trash_root(cfg)
    rel = path.relative_to(root.resolved_path)
    dest = trash / batch / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    counter = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}-{counter}{dest.suffix}")
        counter += 1
    shutil.move(str(path), str(dest))
    return str(dest.relative_to(trash / batch))
