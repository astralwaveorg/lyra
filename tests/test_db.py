"""SQLite 状态库的测试。

为什么测这些：

状态库是「幂等」与「可恢复」的落点。这里锁死三类语义，每一类出错都会造成
难以察觉但代价很高的后果：

1. **幂等**：重复扫描不得重复入队、重复建资产、重复建根。
   不幂等意味着每次全量扫描都会放大工作量，甚至重复发网络请求。

2. **负缓存的清理边界**：`purge_expired_negative_cache` 只允许清
   `not_found` / `no_synced_lyrics`。**`needs_review` 保存的是待人工复核的候选，
   不是失败重试项**——把它当负缓存删掉，用户辛苦复核的上下文就没了；
   `transient_failed` 是技术失败，删掉会让「网络抖一下」被记成「查无此歌」。

3. **租约与未完成事务**：崩溃恢复完全依赖它们。`unfinished_transactions`
   必须只返回真正未完成的，否则恢复流程会对已完成的事务重复动作。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.db import (
    SCHEMA_VERSION,
    DatabaseError,
    JobState,
    ResolutionOutcome,
    StateStore,
    TransactionState,
    open_store,
)

NOW = 1_000_000.0


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_迁移到当前版本且幂等(store: StateStore):
    """迁移必须可重复执行；重复迁移会破坏「升级即重启」的部署方式。"""
    version = store.migrate()
    assert version == SCHEMA_VERSION
    assert store.current_schema_version() == SCHEMA_VERSION
    assert store.migrate() == SCHEMA_VERSION  # 第二次不应报错或改变版本


def test_完整性检查通过(store: StateStore):
    assert store.integrity_check() == "ok"


def test_schema版本高于程序支持时报错(tmp_path: Path):
    """旧程序打开新库必须拒绝，否则会用旧语义误读新结构。"""
    path = tmp_path / "future"
    handle = open_store(path)
    handle.conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 99),)
    )
    try:
        with pytest.raises(DatabaseError) as excinfo:
            handle.migrate()
        assert "高于本程序支持" in str(excinfo.value)
    finally:
        handle.close()


def test_open_store会自动建目录并迁移(tmp_path: Path):
    handle = open_store(tmp_path / "nested" / "data")
    try:
        assert (tmp_path / "nested" / "data").is_dir()
        assert handle.current_schema_version() == SCHEMA_VERSION
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_upsert_root幂等(store: StateStore):
    first = store.upsert_root(name="main", role="library", path="/a")
    second = store.upsert_root(name="main", role="library", path="/a")
    assert first == second
    assert store.table_counts()["roots"] == 1


def test_upsert_root会更新已有记录(store: StateStore):
    """同名根的路径变更（例如容器内外路径调整）必须生效，而不是被忽略。"""
    root_id = store.upsert_root(name="main", role="library", path="/old")
    again = store.upsert_root(name="main", role="library", path="/new", read_only=True)
    assert again == root_id
    row = store.conn.execute("SELECT path, read_only FROM roots WHERE id=?", (root_id,)).fetchone()
    assert row["path"] == "/new"
    assert row["read_only"] == 1
    assert store.table_counts()["roots"] == 1


def test_get_root_id(store: StateStore):
    root_id = store.upsert_root(name="main", role="library", path="/a")
    assert store.get_root_id("main") == root_id
    assert store.get_root_id("missing") is None


def test_upsert_asset按哈希与格式去重(store: StateStore):
    first = store.upsert_asset(audio_hash="h1", media_format="flac", duration=1.0)
    second = store.upsert_asset(audio_hash="h1", media_format="flac", duration=2.0)
    assert first == second, "同一录音数据只应有一个资产"
    assert store.table_counts()["assets"] == 1


def test_upsert_asset区分不同格式(store: StateStore):
    """同一段音频以不同容器存在时是不同资产，不能混为一个。"""
    flac = store.upsert_asset(audio_hash="h1", media_format="flac", duration=1.0)
    mp3 = store.upsert_asset(audio_hash="h1", media_format="mp3", duration=1.0)
    assert flac != mp3
    assert store.table_counts()["assets"] == 2


def test_enqueue_job幂等(store: StateStore):
    first = store.enqueue_job(job_key="k1", job_type="fetch")
    second = store.enqueue_job(job_key="k1", job_type="fetch")
    assert first == second
    assert store.table_counts()["jobs"] == 1


def test_enqueue_job不覆盖已有任务状态(store: StateStore):
    """已领取/已完成的任务不能因为再入队而被重置回 pending。

    否则一次全量扫描会把正在处理的任务重新排队，导致重复请求与重复写入。
    """
    job_id = store.enqueue_job(job_key="k1", job_type="fetch")
    store.finish_job(job_id, state=JobState.DONE)
    store.enqueue_job(job_key="k1", job_type="fetch")
    state = store.conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()["state"]
    assert state == JobState.DONE


def test_upsert_location幂等(store: StateStore):
    root_id = store.upsert_root(name="main", role="library", path="/a")
    first = store.upsert_location(root_id=root_id, rel_path="x.flac", size=1, mtime=1.0)
    second = store.upsert_location(root_id=root_id, rel_path="x.flac", size=2, mtime=2.0)
    assert first == second
    assert store.table_counts()["locations"] == 1


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


def test_同一租约内不可重复领取(store: StateStore):
    """防重复提交同一计划：一个任务在同一时刻只能被一个 worker 持有。"""
    store.enqueue_job(job_key="only", job_type="fetch", next_run_at=NOW - 1)
    first = store.claim_job(owner="w1", lease_seconds=3600, now=NOW)
    assert first is not None and first["job_key"] == "only"
    assert store.claim_job(owner="w2", lease_seconds=3600, now=NOW) is None


def test_领取会记录租约与尝试次数(store: StateStore):
    store.enqueue_job(job_key="only", job_type="fetch", next_run_at=NOW - 1)
    store.claim_job(owner="w1", lease_seconds=60, now=NOW)
    row = store.conn.execute("SELECT * FROM jobs WHERE job_key='only'").fetchone()
    assert row["state"] == JobState.LEASED
    assert row["lease_owner"] == "w1"
    assert row["lease_expires_at"] == pytest.approx(NOW + 60)
    assert row["attempts"] == 1


def test_未到期的任务不会被领取(store: StateStore):
    store.enqueue_job(job_key="later", job_type="fetch", next_run_at=NOW + 3600)
    assert store.claim_job(owner="w1", lease_seconds=60, now=NOW) is None


def test_领取后未完成任务在回收后可再领取(store: StateStore):
    """进程崩溃后租约过期，任务必须能被其他 worker 接管（可恢复）。"""
    store.enqueue_job(job_key="only", job_type="fetch", next_run_at=NOW - 1)
    store.claim_job(owner="w1", lease_seconds=60, now=NOW)

    assert store.recover_expired_leases(now=NOW + 30) == 0, "未过期不该被回收"
    assert store.claim_job(owner="w2", lease_seconds=60, now=NOW + 30) is None

    assert store.recover_expired_leases(now=NOW + 61) == 1
    reclaimed = store.claim_job(owner="w2", lease_seconds=60, now=NOW + 62)
    assert reclaimed is not None and reclaimed["job_key"] == "only"
    # 尝试次数记在库里，用于驱动有限复查计划
    row = store.conn.execute("SELECT attempts, lease_owner FROM jobs WHERE job_key='only'").fetchone()
    assert row["attempts"] == 2, "接管应累加尝试次数"
    assert row["lease_owner"] == "w2"


def test_claim_job返回的是更新后的最新状态(store: StateStore):
    """契约：claim_job 返回的行必须是 UPDATE **之后**的真实状态。

    早期实现返回 UPDATE 前的 SELECT 快照，里面的 attempts / state /
    lease_expires_at 全是旧值，调用方据此做重试计数会少算一次。
    这条测试锁死「返回即可用」，避免再次退化。
    """
    store.enqueue_job(job_key="only", job_type="fetch", next_run_at=NOW - 1)
    claimed = store.claim_job(owner="w1", lease_seconds=60, now=NOW)

    assert claimed["attempts"] == 1, "返回行必须已累加尝试次数"
    assert claimed["state"] == JobState.LEASED
    assert claimed["lease_owner"] == "w1"
    assert claimed["lease_expires_at"] == pytest.approx(NOW + 60)

    fresh = store.conn.execute("SELECT * FROM jobs WHERE job_key='only'").fetchone()
    assert fresh["attempts"] == claimed["attempts"], "返回行与库内必须一致"


def test_finish_job释放租约(store: StateStore):
    job_id = store.enqueue_job(job_key="k", job_type="fetch", next_run_at=NOW - 1)
    store.claim_job(owner="w1", lease_seconds=60, now=NOW)
    store.finish_job(job_id, state=JobState.DONE)
    row = store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["state"] == JobState.DONE


def test_活跃租约不应挡住其他待领取任务(store: StateStore):
    store.enqueue_job(job_key="a", job_type="fetch", priority=10, next_run_at=NOW - 10)
    store.enqueue_job(job_key="b", job_type="fetch", priority=5, next_run_at=NOW - 5)

    first = store.claim_job(owner="w1", lease_seconds=3600, now=NOW)
    assert first is not None and first["job_key"] == "a"

    second = store.claim_job(owner="w2", lease_seconds=3600, now=NOW)
    assert second is not None, "另一个 pending 任务应能被领取"
    assert second["job_key"] == "b"


# ---------------------------------------------------------------------------
# 负缓存
# ---------------------------------------------------------------------------


def _resolution(store: StateStore, key: str, outcome: str, retry_at: float | None) -> None:
    store.record_resolution(
        resolution_key=key, provider="qq", outcome=outcome, next_retry_at=retry_at
    )


def test_负缓存只清可重试的两类(store: StateStore):
    """核心语义：只有 not_found / no_synced_lyrics 是可重试的负结果。"""
    _resolution(store, "nf", ResolutionOutcome.NOT_FOUND, NOW - 1)
    _resolution(store, "ns", ResolutionOutcome.NO_SYNCED_LYRICS, NOW - 1)
    _resolution(store, "nr", ResolutionOutcome.NEEDS_REVIEW, NOW - 1)
    _resolution(store, "tf", ResolutionOutcome.TRANSIENT_FAILED, NOW - 1)
    _resolution(store, "si", ResolutionOutcome.SEARCH_INCOMPLETE, NOW - 1)

    assert store.purge_expired_negative_cache(now=NOW) == 2

    left = {
        row["resolution_key"]
        for row in store.conn.execute("SELECT resolution_key FROM resolutions")
    }
    assert left == {"nr", "tf", "si"}, f"不该被清的记录被删了: {left}"


def test_needs_review不能被当负缓存清理(store: StateStore):
    """`needs_review` 保存候选供**离线复核**，不是失败重试项（设计约束）。

    清掉它，用户复核所需的上游候选与证据就没了，而重新下载既慢又可能再次 503。
    """
    _resolution(store, "review-me", ResolutionOutcome.NEEDS_REVIEW, NOW - 99999)
    assert store.purge_expired_negative_cache(now=NOW) == 0
    assert store.get_resolution("review-me") is not None


def test_技术失败不能被当负缓存清理(store: StateStore):
    """网络抖动不能被记成「查无此歌」。"""
    _resolution(store, "net-fail", ResolutionOutcome.TRANSIENT_FAILED, NOW - 99999)
    assert store.purge_expired_negative_cache(now=NOW) == 0
    assert store.get_resolution("net-fail") is not None


def test_未到期的负缓存不清理(store: StateStore):
    """负缓存必须撑到计划的重试时间点；提前清掉会造成重复请求。"""
    _resolution(store, "future", ResolutionOutcome.NOT_FOUND, NOW + 99999)
    assert store.purge_expired_negative_cache(now=NOW) == 0
    assert store.get_resolution("future") is not None


def test_没有重试时间的负缓存不清理(store: StateStore):
    """`next_retry_at IS NULL` 表示「不计划重试」，不应被当过期清掉。"""
    _resolution(store, "parked", ResolutionOutcome.NOT_FOUND, None)
    assert store.purge_expired_negative_cache(now=NOW) == 0
    assert store.get_resolution("parked") is not None


def test_record_resolution会累加尝试次数(store: StateStore):
    """尝试次数驱动 7/30/90 天的有限复查计划，不能丢。"""
    _resolution(store, "k", ResolutionOutcome.NOT_FOUND, NOW + 1)
    _resolution(store, "k", ResolutionOutcome.NOT_FOUND, NOW + 2)
    row = store.get_resolution("k")
    assert row["attempts"] == 2
    assert row["next_retry_at"] == pytest.approx(NOW + 2)


# ---------------------------------------------------------------------------
# 事务
# ---------------------------------------------------------------------------


def _txn(store: StateStore, txn_id: str, location_id: int, state: str) -> None:
    store.begin_transaction_record(
        txn_id=txn_id,
        location_id=location_id,
        plan={"note": txn_id},
        original_full_hash=None,
        original_audio_hash=None,
    )
    store.set_transaction_state(txn_id, state)


def test_未完成事务只返回未完结的(store: StateStore):
    """崩溃恢复依此判断要不要动作；把终态算进来会重复处理。

    `recovery_conflict` **也是终态**：目标既非原始、也非本事务结果，
    且无备份材料——已无从判定。早先漏了它，于是被标为终态的条目
    每次启动都重新出现在恢复报告里，报告久了就没人看了。
    """
    root_id = store.upsert_root(name="main", role="library", path="/a")
    location_id = store.upsert_location(root_id=root_id, rel_path="x.flac", size=1, mtime=1.0)

    _txn(store, "t_completed", location_id, TransactionState.COMPLETED)
    _txn(store, "t_rolled", location_id, TransactionState.ROLLED_BACK)
    _txn(store, "t_prepared", location_id, TransactionState.PREPARED)
    _txn(store, "t_conflict", location_id, TransactionState.RECOVERY_CONFLICT)
    _txn(store, "t_audio", location_id, TransactionState.AUDIO_COMMITTED)

    unfinished = sorted(row["txn_id"] for row in store.unfinished_transactions())
    # t_conflict 是终态，不该出现在未完成列表里
    assert unfinished == ["t_audio", "t_prepared"]


def test_事务状态推进保留已有哈希(store: StateStore):
    """set_transaction_state 用 COALESCE，不能在只更新状态时抹掉已记录的哈希。"""
    root_id = store.upsert_root(name="main", role="library", path="/a")
    location_id = store.upsert_location(root_id=root_id, rel_path="x.flac", size=1, mtime=1.0)
    store.begin_transaction_record(
        txn_id="t1",
        location_id=location_id,
        plan={},
        original_full_hash="orig-full",
        original_audio_hash="orig-audio",
    )
    store.set_transaction_state("t1", TransactionState.BACKUP_DONE, backup_ref="b1")
    store.set_transaction_state("t1", TransactionState.STAGED, staging_ref="s1")

    row = store.conn.execute("SELECT * FROM transactions WHERE txn_id='t1'").fetchone()
    assert row["state"] == TransactionState.STAGED
    assert row["original_full_hash"] == "orig-full"
    assert row["original_audio_hash"] == "orig-audio"
    assert row["backup_ref"] == "b1"
    assert row["staging_ref"] == "s1"


# ---------------------------------------------------------------------------
# sidecar 写入记录
# ---------------------------------------------------------------------------


def test_sidecar写入记录与撤销标记(store: StateStore):
    write_id = store.record_sidecar_write(
        sidecar_path="/a/x.lrc", content_hash="h", created_new=True, previous_hash=None
    )
    last = store.last_sidecar_write("/a/x.lrc")
    assert last["id"] == write_id
    assert last["undone_at"] is None

    store.mark_sidecar_undone(write_id)
    assert store.last_sidecar_write("/a/x.lrc") is None, "已撤销的不该再被当作最新写入"


def test_last_sidecar_write取最新一条(store: StateStore):
    store.record_sidecar_write(
        sidecar_path="/a/x.lrc", content_hash="old", created_new=True, previous_hash=None
    )
    store.record_sidecar_write(
        sidecar_path="/a/x.lrc", content_hash="new", created_new=False, previous_hash="old"
    )
    assert store.last_sidecar_write("/a/x.lrc")["content_hash"] == "new"


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


def test_规则增删与过期(store: StateStore):
    """排除规则必须带作用域与理由（设计约束）。"""
    rule_id = store.add_rule(scope="path", target="album/x.flac", action="skip_all",
                             reason="用户确认纯音乐")
    targets = [row["target"] for row in store.iter_rules(now=NOW)]
    assert "album/x.flac" in targets

    store.add_rule(scope="path", target="album/y.flac", action="skip_fetch",
                   reason="临时", expires_at=NOW - 1)
    assert "album/y.flac" not in [row["target"] for row in store.iter_rules(now=NOW)]

    assert store.remove_rule(rule_id) is True
    assert store.remove_rule(rule_id) is False


def test_规则按作用域目标动作去重(store: StateStore):
    """同一 (scope, target, action) 只应有一条；重复添加更新理由而不是堆叠。"""
    store.add_rule(scope="path", target="t", action="skip_all", reason="第一次")
    store.add_rule(scope="path", target="t", action="skip_all", reason="第二次")
    rows = list(store.iter_rules(now=NOW))
    assert len(rows) == 1
    assert rows[0]["reason"] == "第二次"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def test_table_counts覆盖所有表(store: StateStore):
    counts = store.table_counts()
    for name in ("roots", "assets", "locations", "jobs", "resolutions", "transactions"):
        assert name in counts
        assert counts[name] == 0


def test_resolve_ids_by_hashes(store: StateStore):
    from lyra.db import resolve_ids_by_hashes

    asset_id = store.upsert_asset(audio_hash="h1", media_format="flac", duration=1.0)
    mapping = resolve_ids_by_hashes(store, ["h1", "missing"])
    assert mapping == {"h1": asset_id}


def test_位置没变化时不写库(tmp_path):
    """监控每 30 秒整库对账，1810 首就是 1810 个写事务，会和写入任务抢锁。

    实际：一次 12 首的写入里 9 首被判「数据库持续被占用」而推迟。
    所以「字段没变、5 分钟内见过」必须**直接返回，不进写事务**。
    """
    import time

    from lyra.db import open_store

    store = open_store(tmp_path / "data")
    root_id = store.upsert_root(name="lib", role="library", path=str(tmp_path / "lib"))
    first = store.upsert_location(
        root_id=root_id, rel_path="a.flac", size=10, mtime=100.0, inode=5)
    seen_before = store.conn.execute(
        "SELECT last_seen_at FROM locations WHERE id=?", (first,)).fetchone()[0]

    time.sleep(0.01)
    again = store.upsert_location(
        root_id=root_id, rel_path="a.flac", size=10, mtime=100.0, inode=5)
    assert again == first
    seen_after = store.conn.execute(
        "SELECT last_seen_at FROM locations WHERE id=?", (first,)).fetchone()[0]
    assert seen_after == seen_before, "没变化却写了库（这正是抢锁的来源）"

    # 变了就必须写
    store.upsert_location(
        root_id=root_id, rel_path="a.flac", size=11, mtime=101.0, inode=5)
    row = store.conn.execute(
        "SELECT size, mtime FROM locations WHERE id=?", (first,)).fetchone()
    assert (row["size"], row["mtime"]) == (11, 101.0)
    store.close()


def test_两个进程同时迁移不会互相撞车(tmp_path):
    """Web 与监控是**两个容器**，启动时都会迁移。

    实际：A 建好 `write_waits` 并提交，B 在等写锁之后仍按旧版本判断，
    重复执行 `CREATE TABLE` → `table write_waits already exists`，容器崩溃。
    迁移必须跨进程串行，并且拿到锁后**重新读版本**。
    """
    import multiprocessing as mp

    from lyra.db import open_store

    def worker(path: str, out: list) -> None:
        store = open_store(path)
        try:
            out.append(store.migrate(backup=False))
        finally:
            store.close()

    data = tmp_path / "data"
    ctx = mp.get_context("fork")   # 避免 spawn 需要可 pickle 的入口函数
    manager = ctx.Manager()
    results = manager.list()
    procs = [ctx.Process(target=worker, args=(str(data), results)) for _ in range(4)]
    for p in procs:
        p.start()
    # 30 秒在机器负载高时不够（例如同时在构建镜像），超时会让 exitcode 变成 None，
    # 报出来像是「迁移撞车」，实际只是没跑完。
    for p in procs:
        p.join(90)
    alive = [index for index, p in enumerate(procs) if p.is_alive()]
    for p in procs:
        if p.is_alive():
            p.kill()
    assert not alive, f"迁移进程超时未退出: {alive}"
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    from lyra.db import SCHEMA_VERSION

    assert list(results) == [SCHEMA_VERSION] * 4
