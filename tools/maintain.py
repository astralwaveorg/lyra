"""运维脚本：清理重复暂存行，并输出当前歌词覆盖情况。

在容器内运行：`docker exec lyra python /data/maintain.py`
"""

from lyra.config import load_config
from lyra.db import open_store
from lyra.stats import collect_stats

cfg = load_config()
store = open_store(cfg.storage.resolved_data_dir)
try:
    removed = store.dedupe_staged()
    print(f"  作废重复暂存行: {removed}")

    print("  暂存状态分布:")
    for row in store.conn.execute(
        "SELECT state, COUNT(*) AS n FROM staged_lyrics GROUP BY state ORDER BY n DESC"
    ):
        print(f"    {row['state']:14} {row['n']}")

    duplicated = store.conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT location_id FROM staged_lyrics WHERE state='staged'"
        "  GROUP BY location_id HAVING COUNT(*) > 1)"
    ).fetchone()["n"]
    print(f"  同位置多份 staged 的组数: {duplicated}（应为 0）")
finally:
    store.close()

for root in cfg.enabled_roots:
    stats = collect_stats(root)
    print()
    print(f"  曲库 {root.name}:")
    print(f"    音乐文件        {stats.audio_total}")
    print(f"    lrc 文件        {stats.lrc_total}（0 字节 {stats.lrc_empty}）")
    print(f"    内嵌有歌词       {stats.embedded_synced}")
    print(f"    有歌词的曲目     {stats.tracks_with_lyrics} / {stats.audio_total}"
          f" = {stats.coverage_pct}%")
    print(f"    仍缺歌词        {stats.tracks_without_lyrics}")
