#!/usr/bin/env python3
"""用应用自身的校验器验证一份配置文件。

修改配置后先运行这个脚本，不要等重启时才发现问题：

    python tools/check_config.py path/to/lyra.toml

它会先检查 TOML 语法，再走 Lyra 的严格校验（未知键、非法取值、重复段等）。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lyra.config import ConfigError, load_config  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2

    # 第一关：TOML 本身能否解析。重复键、缺引号这类问题在这里就暴露。
    try:
        with path.open("rb") as fh:
            tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ TOML 语法错误：{exc}")
        print("  提示：重复键通常意味着段头被误删，后面的键并进了上一段。")
        return 1

    # 第二关：应用自己的严格校验（未知键会报错并给出拼写建议）。
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(f"✗ 配置校验失败：\n{exc}")
        return 1

    print(f"✓ {path} 通过校验")
    print(f"  受管根: {[r.name for r in cfg.enabled_roots]}")
    print(f"  来源:   {cfg.providers.enabled}")
    print(f"  网易云: {cfg.providers.netease.base_url or '（未配置，走公网旧接口）'}")
    print(f"  MusicBrainz: {cfg.providers.musicbrainz.base_url or '（未配置）'}")
    print(f"  写入开关: {'开' if cfg.policy.write_enabled else '关'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
