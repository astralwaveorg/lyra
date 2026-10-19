"""目录被改层级之后，库里不能继续把已不存在的路径当成曲目。

## 为什么必须有

用户把目录去掉了一层（`G.E.M. 鄧紫棋/G.E.M. 鄧紫棋/18…/` → `G.E.M. 鄧紫棋/18…/`），
结果状态库里留下 219 条指向已删路径的记录（423 条 vs 磁盘 204 个文件），
而且 `missing_since` 全是 NULL —— 也就是说**同步只补不删**，
界面上把它们照旧列成曲目，计数也跟着虚高。

两条规则被这份测试钉住：
1. 整库核对时，本次没见到的旧位置要被标记为缺失（只标记、不删记录，
   因为文件可能是临时移走或挂载掉线）。
2. 增量核对（只扫几个文件）**绝不能**把其余全部标成缺失。
"""

from __future__ import annotations

from pathlib import Path

from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import sync_state


def _setup(tmp_path: Path, flac_factory):
    lib = tmp_path / "lib"
    flac_factory(lib / "旧目录" / "a.flac", entries=(("TITLE", "A"),))
    flac_factory(lib / "旧目录" / "b.flac", entries=(("TITLE", "B"),))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)
    return cfg, store, lib


def _present(store) -> set[str]:
    return {
        r["rel_path"]
        for r in store.conn.execute(
            "SELECT rel_path FROM locations WHERE missing_since IS NULL"
        )
    }


def test_文件移动后记录跟着走而不是标记缺失(tmp_path, flac_factory):
    """**这是本次需求的核心行为**。

    用户原话：「我移动、删除、重命名后，能够自动识别新文件……而不是标记缺失！」
    所以移动后应当把旧记录**改到新路径**（location_id 不变，历史跟着走），
    而不是「旧行标缺失 + 新建一行」——后者会让界面上多出一条不存在的曲目，
    并且把待复核、来源统计这些历史留在查不到的行上。
    """
    cfg, store, lib = _setup(tmp_path, flac_factory)
    old_id = store.conn.execute(
        "SELECT id FROM locations WHERE rel_path='旧目录/a.flac'"
    ).fetchone()["id"]

    (lib / "旧目录" / "a.flac").rename(lib / "a.flac")

    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)

    assert _present(store) == {"a.flac", "旧目录/b.flac"}, _present(store)
    rows = {
        r["rel_path"]: r
        for r in store.conn.execute("SELECT id, rel_path FROM locations")
    }
    assert "旧目录/a.flac" not in rows, "旧路径不该再留下一条记录"
    assert rows["a.flac"]["id"] == old_id, "应当是同一行改了路径，而不是新建一行"
    assert len(rows) == 2, f"不该产生重复记录，实际 {sorted(rows)}"
    store.close()


def test_文件真的被删除时标记缺失(tmp_path, flac_factory):
    """移动要跟随，删除才标缺失——两者不能混为一谈。"""
    cfg, store, lib = _setup(tmp_path, flac_factory)
    (lib / "旧目录" / "b.flac").unlink()

    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)

    assert _present(store) == {"旧目录/a.flac"}, _present(store)
    row = store.conn.execute(
        "SELECT missing_since FROM locations WHERE rel_path='旧目录/b.flac'"
    ).fetchone()
    assert row is not None, "删除不等于抹掉记录"
    assert row["missing_since"] is not None, "删除后应标记为缺失"
    store.close()


def test_增量核对不会把其余文件标成缺失(tmp_path, flac_factory):
    """监听事件触发的批次只扫一两个文件；把它当整库核对会误标全库。"""
    cfg, store, lib = _setup(tmp_path, flac_factory)

    sync_state(
        store, cfg, cfg.roots[0],
        audit_root(cfg.roots[0], only_rel_paths={"旧目录/a.flac"}),
        full_scan=False,
    )
    assert _present(store) == {"旧目录/a.flac", "旧目录/b.flac"}, "增量核对误标了缺失"
    store.close()


def test_整库抓取也会识别移动(tmp_path, flac_factory):
    """走真实的 run_fetch 调用链，确认 `full_scan` 真的被传下去了。

    （只测 `sync_state` 是不够的：把 run_fetch 里的 `full_scan` 写死成 False，
    上面两条测试照样通过——它们直接调 sync_state。这条才验接线。）
    """
    from lyra.pipeline import run_fetch

    cfg, store, lib = _setup(tmp_path, flac_factory)
    (lib / "旧目录" / "a.flac").rename(lib / "a.flac")

    # 空来源列表：不联网，只走「审计 → 同步」，这正是我们要验的那一段
    run_fetch(store, cfg, cfg.roots[0], providers=[])

    # 走 run_fetch 时也应识别为「移动」并更新路径
    assert _present(store) == {"a.flac", "旧目录/b.flac"}, _present(store)
    store.close()


def test_文件改名后记录跟着走(tmp_path, flac_factory):
    """改名（文件名变了）同样必须跟随，不能「删一条 + 加一条」。

    为什么单独一条：上面那条移动测试里文件名没变，靠 `(文件名, 大小)`
    兜底就能过。**改名恰恰把文件名改掉了**，兜底救不了，只能靠 inode。
    而 inode 这一列长期是 NULL——`upsert_location` 接受 inode 参数，
    却没有任何调用方传过它（实际库里 0 行有 inode），
    于是「按 inode 识别改名」这条路径从未执行过。
    """
    cfg, store, lib = _setup(tmp_path, flac_factory)
    old_id = store.conn.execute(
        "SELECT id FROM locations WHERE rel_path='旧目录/a.flac'"
    ).fetchone()["id"]

    (lib / "旧目录" / "a.flac").rename(lib / "旧目录" / "改名了 a.flac")

    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)

    assert _present(store) == {"旧目录/改名了 a.flac", "旧目录/b.flac"}, _present(store)
    rows = {
        r["rel_path"]: r
        for r in store.conn.execute("SELECT id, rel_path FROM locations")
    }
    assert "旧目录/a.flac" not in rows, "旧路径不该继续留着（哪怕只是标成缺失）"
    assert rows["旧目录/改名了 a.flac"]["id"] == old_id, "应当同一行改路径，而不是新建"
    assert len(rows) == 2, f"不该产生重复记录，实际 {sorted(rows)}"
    store.close()


def test_扫描时把_inode_写进库(tmp_path, flac_factory):
    """根因守卫：inode 不写进库，改名识别就是死代码。

    这条断言看着琐碎，但它守的正是上面那个 bug 的源头——
    参数加了、没人传，类型检查看不出来，只有查库才发现。
    """
    cfg, store, _lib = _setup(tmp_path, flac_factory)
    rows = store.conn.execute("SELECT rel_path, inode FROM locations").fetchall()
    assert rows
    for row in rows:
        assert row["inode"], f"{row['rel_path']} 的 inode 没有写进库"
    store.close()


def test_对账要报出新出现的具体文件(tmp_path, flac_factory):
    """只给「新增 1 个」这种计数不够用。

    监控拿到计数之后没法对这些文件做事，于是「新增音乐自动补 .lrc /
    自动刮削」永远只能等下一次文件事件——实际批量拷入 176 首之后，
    事件只覆盖了一部分，其余一直停在「有内嵌、无 .lrc」。
    所以 rescan 必须把**路径**交出来。
    """
    from lyra.pipeline import rescan

    cfg, store, lib = _setup(tmp_path, flac_factory)
    flac_factory(lib / "新目录" / "新歌.flac", entries=(("TITLE", "新歌"),))

    result = rescan(store, cfg, cfg.roots[0])

    assert result["added"] == 1, result
    assert result["added_paths"] == ["新目录/新歌.flac"], result
    assert result["present_after"] == 3
    # 没有变化时不该报「新增」
    again = rescan(store, cfg, cfg.roots[0])
    assert again["added"] == 0 and again["added_paths"] == []
    store.close()
