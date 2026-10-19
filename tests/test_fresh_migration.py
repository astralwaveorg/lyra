"""全新数据库的迁移路径。

## 为什么单独测这个

已有库的迁移在开发过程中被反复验证过（每次加迁移都会在真实库上跑）。
但**全新部署走的是另一条路径**：1→9 一次性全部执行。

如果中间任何一步有问题，新用户**第一次启动就会失败**——而这条路径
在日常开发中几乎不会被走到，因为开发用的库早就迁移完了。

这是上线前必须单独验证的东西。
"""

from __future__ import annotations

import pytest

from lyra.db import SCHEMA_VERSION, open_store

#: 全新库应当具备的表（`auth_users` / `auth_tokens` 由鉴权模块建，一并列出）
_EXPECTED_TABLES = {
    "meta", "roots", "assets", "locations", "metadata_revisions",
    "lyric_observations", "rules", "jobs", "resolutions", "approvals",
    "transactions", "sidecar_writes", "staged_lyrics", "renames",
    "tasks", "write_locks", "watch_log", "auth_users", "auth_tokens",
}

#: 各次迁移新增的列——任一缺失都说明那条迁移没跑到
_MIGRATED_COLUMNS = [
    ("staged_lyrics", "origin"),        # 迁移 5
    ("tasks", "state"),                 # 迁移 6
    ("write_locks", "expires_at"),      # 迁移 7
    ("resolutions", "location_id"),     # 迁移 8
    ("watch_log", "counters_json"),     # 迁移 9
]


@pytest.fixture
def fresh(tmp_path):
    store = open_store(tmp_path / "fresh")
    yield store
    store.close()


def test_全新库能跑完全部迁移(fresh):
    assert fresh.current_schema_version() == SCHEMA_VERSION


def test_全新库具备全部表(fresh):
    actual = {
        r[0] for r in fresh.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert not (_EXPECTED_TABLES - actual), f"缺表：{sorted(_EXPECTED_TABLES - actual)}"


@pytest.mark.parametrize("table,column", _MIGRATED_COLUMNS)
def test_每条迁移新增的列都存在(fresh, table, column):
    """逐条固定。某条迁移漏跑时，这里会直接指出是哪一列缺失。"""
    columns = {r[1] for r in fresh.conn.execute(f"PRAGMA table_info({table})")}
    assert column in columns, f"{table}.{column} 缺失——对应迁移没有执行"


def test_全新库能正常读写(fresh):
    root_id = fresh.upsert_root(name="r", role="library", path="/tmp")
    location_id = fresh.upsert_location(
        root_id=root_id, rel_path="a.flac", size=1, mtime=1.0
    )
    fresh.log_watch_event("测试", "全新库")
    fresh.record_resolution(
        resolution_key="k", provider="qq", outcome="not_found",
        location_id=location_id,
    )
    fresh.acquire_write_lock("write", "测试", ttl_seconds=60)

    assert fresh.get_meta("不存在", "默认") == "默认"
    assert len(fresh.recent_watch_events(5)) == 1
    assert fresh.write_lock_holder("write") == "测试"


def test_重复打开不重跑迁移也不丢数据(tmp_path):
    """幂等性。重跑迁移可能报错或损坏数据，必须确认只跑一次。"""
    path = tmp_path / "again"
    first = open_store(path)
    root_id = first.upsert_root(name="r", role="library", path="/tmp")
    first.upsert_location(root_id=root_id, rel_path="a.flac", size=1, mtime=1.0)
    first.close()

    second = open_store(path)
    try:
        assert second.current_schema_version() == SCHEMA_VERSION
        count = second.conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        assert count == 1, "重复打开后数据丢失"
    finally:
        second.close()
