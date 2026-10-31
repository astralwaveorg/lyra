"""整理移动时目标路径已有位置行 → 合并而不是报 500。

## 真实故障（生产日志）

整理页「一键整理」移动文件报 `Internal Server Error`，容器日志：

```
File "lyra/db.py", line 2429, in update_location_path
sqlite3.IntegrityError: UNIQUE constraint failed: locations.root_id, locations.rel_path
```

## 根因

曲库里有一批**脏目录名**（`G.E.M. 鄧紫棋; G.E.M.鄧紫棋/…`——下载工具
把两个同名目录拼在一起）。扫描早就为**规范路径**登记了位置行；之后
full 整理把文件从脏目录移到规范目录时，旧的（脏路径）行要改成新路径，
而新路径已有行——`UNIQUE(root_id, rel_path)` 直接冲突，整个接口 500。

## 修法

`update_location_path` 发现目标路径已有行时**合并**：把旧行携带的
歌词观察、暂存歌词、标签提案、解析记录迁到存活行，再删旧行。既有
状态不丢，也不留重复行。
"""

from __future__ import annotations

from pathlib import Path

from lyra.db import open_store


def _store(tmp_path: Path):
    store = open_store(tmp_path / "data")
    root_id = store.upsert_root(
        name="lib", role="library", path=str(tmp_path / "lib"),
        enabled=True, read_only=False,
    )
    return store, root_id


def test_目标路径已有行时合并而不报错(tmp_path: Path):
    store, root_id = _store(tmp_path)
    # 脏目录行（本次移动的源）
    stale = store.upsert_location(
        root_id=root_id, rel_path="脏目录/歌.flac", size=10, mtime=1.0,
    )
    # 规范路径行（扫描早已登记，存活）
    live = store.upsert_location(
        root_id=root_id, rel_path="G.E.M. 鄧紫棋/Revelación/07 - Pasión.flac",
        size=10, mtime=2.0,
    )
    store.record_lyric_observation(
        location_id=stale, embedded_state="synced", sidecar_state="synced",
    )
    store.stage_lyrics(
        location_id=stale, content_hash="h1", content="[00:01.00]词\n",
        provider="qq",
    )

    # 移动：脏路径 → 规范路径（目标已有行）
    store.update_location_path(stale, "G.E.M. 鄧紫棋/Revelación/07 - Pasión.flac")

    rows = store.conn.execute(
        "SELECT id, rel_path, missing_since FROM locations WHERE root_id=?",
        (root_id,),
    ).fetchall()
    assert len(rows) == 1, f"应合并成一行，实际 {len(rows)} 行"
    assert rows[0]["id"] == live
    assert rows[0]["missing_since"] is None

    # 附属数据迁到了存活行，没丢
    obs = store.conn.execute(
        "SELECT COUNT(*) n FROM lyric_observations WHERE location_id=?",
        (live,),
    ).fetchone()
    assert obs["n"] == 1, "歌词观察应迁到存活行"
    staged = store.conn.execute(
        "SELECT COUNT(*) n FROM staged_lyrics WHERE location_id=?", (live,),
    ).fetchone()
    assert staged["n"] == 1, "暂存歌词应迁到存活行"
    store.close()


def test_目标路径没有行时照常改路径(tmp_path: Path):
    store, root_id = _store(tmp_path)
    loc = store.upsert_location(
        root_id=root_id, rel_path="旧/歌.flac", size=10, mtime=1.0,
    )
    store.update_location_path(loc, "新/歌.flac")
    row = store.conn.execute(
        "SELECT rel_path FROM locations WHERE id=?", (loc,)
    ).fetchone()
    assert row["rel_path"] == "新/歌.flac"
    store.close()


def test_合并后不留下重复行(tmp_path: Path):
    """反复移动同一首歌（整理 → 再整理）不应累积重复行。"""
    store, root_id = _store(tmp_path)
    a = store.upsert_location(root_id=root_id, rel_path="a/歌.flac", size=1, mtime=1.0)
    store.upsert_location(root_id=root_id, rel_path="b/歌.flac", size=1, mtime=2.0)
    store.update_location_path(a, "b/歌.flac")
    n = store.conn.execute(
        "SELECT COUNT(*) n FROM locations WHERE root_id=?", (root_id,)
    ).fetchone()["n"]
    assert n == 1
    store.close()


def test_合并时附属表有重复数据也能完成(tmp_path: Path):
    """旧行与存活行都有同内容的暂存歌词 → 保留一份，合并仍要成功。"""
    store, root_id = _store(tmp_path)
    stale = store.upsert_location(
        root_id=root_id, rel_path="脏/歌.flac", size=10, mtime=1.0,
    )
    live = store.upsert_location(
        root_id=root_id, rel_path="干净/歌.flac", size=10, mtime=2.0,
    )
    # 两边各有一条内容相同的暂存歌词（content_hash 相同）
    store.stage_lyrics(
        location_id=stale, content_hash="same", content="[00:01.00]词\n",
        provider="qq",
    )
    store.stage_lyrics(
        location_id=live, content_hash="same", content="[00:01.00]词\n",
        provider="qq",
    )
    store.upsert_tag_proposal(
        location_id=stale, state="review", source="search", confidence=0.8,
        title="歌", artists="人", payload={"rows": []},
    )
    store.upsert_tag_proposal(
        location_id=live, state="review", source="search", confidence=0.8,
        title="歌", artists="人", payload={"rows": []},
    )

    store.update_location_path(stale, "干净/歌.flac")

    n = store.conn.execute(
        "SELECT COUNT(*) AS n FROM locations WHERE root_id=?", (root_id,)
    ).fetchone()["n"]
    assert n == 1, "应合并成一行"
    staged = store.conn.execute(
        "SELECT COUNT(*) n FROM staged_lyrics WHERE location_id=?", (live,)
    ).fetchone()["n"]
    assert staged == 1, f"同内容暂存应只剩一份，实际 {staged}"
    props = store.conn.execute(
        "SELECT COUNT(*) n FROM tag_proposals WHERE location_id=?", (live,)
    ).fetchone()["n"]
    assert props == 1, f"标签提案应只剩一份，实际 {props}"
    store.close()


# ---------------------------------------------------------------------------
# 目录名不因「同一信息写在两个字段」而变成 `A; A`
# ---------------------------------------------------------------------------


def test_别名字段重复不产生分号拼接目录():
    """`ALBUMARTISTS` 与 `ALBUMARTIST` 是同一条信息，且一边简繁。

    不去重就会拼成 `G.E.M. 鄧紫棋; G.E.M.鄧紫棋` —— 曲库里已经有一批
    这种脏目录，整理越理越乱。
    """
    from lyra.organize import _artist_list, _values

    tags = {
        "ALBUMARTISTS": ["G.E.M. 鄧紫棋"],
        "ALBUMARTIST": ["G.E.M.鄧紫棋"],
    }
    assert _values(tags, "ALBUMARTISTS", "ALBUMARTIST") == ["G.E.M. 鄧紫棋"]
    assert _artist_list(tags) == ["G.E.M. 鄧紫棋"]


def test_不同艺人仍按分号拆开():
    from lyra.organize import _artist_list

    assert _artist_list({"ARTIST": ["A; B"]}) == ["A", "B"]
    # 斜杠不拆：AC/DC 是一个乐队
    assert _artist_list({"ARTIST": ["AC/DC"]}) == ["AC/DC"]


def test_归位目标不含分号拼接(tmp_path: Path):
    from lyra.organize import target_rel_path

    tags = {
        "ALBUMARTISTS": ["G.E.M. 鄧紫棋"],
        "ALBUMARTIST": ["G.E.M.鄧紫棋"],
        "ALBUM": ["25 LOOKS"],
        "TITLE": ["再見"],
        "TRACKNUMBER": ["1"],
    }
    rel, _fields = target_rel_path(
        tags=tags, source=tmp_path / "x.flac", suffix=".flac",
    )
    assert ";" not in rel, f"目录名不应含分号拼接：{rel}"
    assert rel.startswith("G.E.M. 鄧紫棋/25 LOOKS/")


def test_入库自动归位同步位置行(tmp_path: Path):
    """自动归位移动文件后必须更新位置行。

    不更新的后果（生产实测 174 首）：位置行还指向旧路径，界面显示
    「文件不存在」「缺歌词」，而文件其实好好地待在新位置——歌词观察、
    暂存提案全部错位。
    """
    from lyra.config import Config, RootConfig, StorageConfig
    from lyra.db import open_store
    from lyra.ingest import _organize_new_files

    lib = tmp_path / "lib"
    src_dir = lib / "乱目录"
    src_dir.mkdir(parents=True)
    from conftest import make_flac

    make_flac(
        src_dir / "歌.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "人"), ("ALBUM", "专辑"),
                 ("TRACKNUMBER", "1")),
    )
    cfg = Config(
        roots=[RootConfig(name="lib", path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    cfg.policy.organize.template = "{artist}/{album}/{track_no} - {title}"
    store = open_store(cfg.storage.resolved_data_dir)
    root_id = store.upsert_root(
        name="lib", role="library", path=str(lib), enabled=True, read_only=False,
    )
    loc = store.upsert_location(
        root_id=root_id, rel_path="乱目录/歌.flac", size=1, mtime=1.0,
    )
    store.upsert_task(
        task_id="task-1", kind="ingest", state="running", started_at=1.0,
    )

    moved = _organize_new_files(
        store, cfg, cfg.roots[0], ["乱目录/歌.flac"], "task-1", root_id,
    )
    assert moved == 1
    row = store.conn.execute(
        "SELECT rel_path FROM locations WHERE id=?", (loc,)
    ).fetchone()
    assert row["rel_path"] != "乱目录/歌.flac", "位置行应跟着移动更新"
    assert (lib / row["rel_path"]).is_file(), "新路径应指向真实文件"
    store.close()
