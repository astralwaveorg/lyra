"""待办队列的计数与列表必须反映**当前状态**，不是历史流水。

## 真实故障（生产实测）

待办「归位」显示 **9508 条**，实际需要人处理的是几十条。原因：

- `task_steps` 是**流水**——每次扫描都为一个文件写一条。同一个冲突反复
  入库，累积成 205 条（同一个文件）；
- `mode=off` 时写入的是「仅生成归置建议」，那是**建议**不是待处理项，
  却被算进了待办。

计数与列表都改成：按文件去重 + 只认真实冲突（detail 里有 conflict）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    store.upsert_location(
        root_id=root_id, rel_path="歌.flac", size=1, mtime=1.0,
    )
    return TestClient(create_app(cfg)), store


def _add_step(store, *, rel_path: str, detail: dict, task_id: str) -> None:
    store.conn.execute(
        "INSERT INTO tasks(id, kind, state, started_at) VALUES(?,?,?,?)",
        (task_id, "ingest", "done", 1.0),
    )
    store.add_task_step(
        task_id=task_id, seq=1, kind="organize", rel_path=rel_path,
        state="review", detail=detail,
    )
    store.conn.commit()


def test_同一冲突重复写入只算一条(tmp_path: Path):
    client, store = _client(tmp_path)
    for index in range(5):
        _add_step(
            store, rel_path="歌.flac",
            detail={"conflict": "新/歌.flac", "fields": {"title": "歌"}},
            task_id=f"t{index}",
        )
    counts = client.get("/api/v1/todo").json()["counts"]
    assert counts["organize"] == 1, f"同一文件只算一条：{counts}"

    page = client.get("/api/v1/todo", params={"kind": "organize"}).json()
    assert len(page["items"]) == 1, f"列表也要去重：{page['items']}"
    store.close()


def test_仅生成建议不计入待办(tmp_path: Path):
    """mode=off 写的是「建议」，不是需要人处理的问题。"""
    client, store = _client(tmp_path)
    _add_step(
        store, rel_path="歌.flac",
        detail={"note": "mode=off：仅生成归置建议（整理页/待办可见），不动文件"},
        task_id="t1",
    )
    counts = client.get("/api/v1/todo").json()["counts"]
    assert counts["organize"] == 0, f"建议不该占待办：{counts}"
    page = client.get("/api/v1/todo", params={"kind": "organize"}).json()
    assert page["items"] == []
    store.close()


def test_不同文件的冲突各算一条(tmp_path: Path):
    client, store = _client(tmp_path)
    store.conn.execute(
        "INSERT INTO tasks(id, kind, state, started_at) VALUES('t1','ingest','done',1.0)"
    )
    store.upsert_location(
        root_id=store.get_root_id("lib"), rel_path="歌2.flac", size=1, mtime=1.0,
    )
    for rel in ("歌.flac", "歌2.flac"):
        store.add_task_step(
            task_id="t1", seq=1, kind="organize", rel_path=rel,
            state="review", detail={"conflict": "新/" + rel, "fields": {}},
        )
    store.conn.commit()
    counts = client.get("/api/v1/todo").json()["counts"]
    assert counts["organize"] == 2, f"不同文件各算一条：{counts}"
    store.close()


def test_任务明细滚动清理(tmp_path: Path):
    """已完成的明细要能清理，否则几个月后几十万行。"""
    client, store = _client(tmp_path)
    store.conn.execute(
        "INSERT INTO tasks(id, kind, state, started_at) VALUES('t1','ingest','done',1.0)"
    )
    for index in range(20):
        store.add_task_step(
            task_id="t1", seq=index, kind="cover", rel_path=f"歌{index}.flac",
            state="done", detail={},
        )
    store.conn.commit()
    removed = store.prune_task_steps(keep=5)
    assert removed == 15, f"应保留 5 条清掉 15 条，实际 {removed}"
    left = store.conn.execute(
        "SELECT COUNT(*) n FROM task_steps WHERE state='done'"
    ).fetchone()["n"]
    assert left == 5
    store.close()


def test_未完成的明细不被清理(tmp_path: Path):
    """review/failed 还是待办队列的来源，删了待办会凭空少条目。"""
    client, store = _client(tmp_path)
    store.conn.execute(
        "INSERT INTO tasks(id, kind, state, started_at) VALUES('t1','ingest','done',1.0)"
    )
    for index in range(10):
        store.add_task_step(
            task_id="t1", seq=index, kind="organize", rel_path=f"歌{index}.flac",
            state="review", detail={"conflict": "x", "fields": {}},
        )
    store.conn.commit()
    store.prune_task_steps(keep=2)
    left = store.conn.execute(
        "SELECT COUNT(*) n FROM task_steps WHERE state='review'"
    ).fetchone()["n"]
    assert left == 10, "未完成的明细要留着"
    store.close()
