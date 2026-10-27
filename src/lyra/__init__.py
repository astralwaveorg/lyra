"""Lyra — 音乐库歌词与标签维护服务。

模块划分与开发方式见 docs/development.md：
    config   配置加载与校验（可移植性的核心）
    models   领域模型与状态枚举
    formats  以真实容器为准的格式识别
    lrc      LRC 解析、分类与规范化
    scan     递归发现
    audit    库审计与健康检查
    pipeline 抓取 → 评分 → 暂存 → 写入
    writer   事务化字节级写入与恢复
    api      HTTP 接口与前端托管
"""

from __future__ import annotations

__version__ = "1.2.0"

__all__ = ["__version__"]
