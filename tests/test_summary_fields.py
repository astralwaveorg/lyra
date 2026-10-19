"""运行结果的结构体字段名必须与引用处一致。

## 为什么需要这个测试

同一个错误犯了**两次**：API 在构造任务结果时写了 `a.written` 和 `a.failed`，
而 `RunSummary` 里实际叫 `applied` 与 `apply_failed`。

而且这个错误**只在写入成功之后、构造结果时才炸**——文件已经写进去了，
任务却报失败。更难查的是：它不像语法错误那样立刻暴露，
只在真正跑起来、走到那一步才出现。

所以这里做**源码级的交叉检查**：把 API 里所有 `a.<属性>` 引用抽出来，
逐个对照 `RunSummary` 的真实字段。这样新增字段引用写错名字会立刻被抓到，
不必等到某次真实运行。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from lyra.pipeline import RunSummary


def test_API引用的RunSummary字段都真实存在():
    source = Path("src/lyra/api.py").read_text(encoding="utf-8")
    valid = {f.name for f in dataclasses.fields(RunSummary)}

    # 只匹配 `a.xxx` 形式——API 里对 run_apply / run_fetch 的返回值统一用 a 命名
    referenced = set(re.findall(r"\ba\.([a-z_][a-z0-9_]*)\b", source))

    unknown = sorted(referenced - valid)
    assert not unknown, (
        "API 引用了 RunSummary 上不存在的字段：\n  "
        + "\n  ".join(f"a.{name}" for name in unknown)
        + f"\n  RunSummary 实际字段：{sorted(valid)}"
    )


def test_已知易错字段名():
    """把遇到过的两个坑固定下来，防止再被改回去。"""
    valid = {f.name for f in dataclasses.fields(RunSummary)}
    assert "applied" in valid, "写入数应叫 applied"
    assert "apply_failed" in valid, "写入失败数应叫 apply_failed"
    assert "written" not in valid, "`written` 不存在——曾因此报 AttributeError"
    assert "failed" not in valid, "`failed` 不存在——曾因此报 AttributeError"


def test_写入与抓取处理同一批曲目():
    """`limit` 必须对两段流程含义一致。

    实际遇到过：传 `limit=5` 想小批量试写，抓取只处理了 5 首，
    **写入却遍历了全部 95 项待写入记录**——对整库动手。
    原因是 run_apply 完全不看 limit。

    修法是让抓取记录下它实际处理过的曲目，写入只作用于这一批。
    """
    import inspect

    from lyra.api import create_app
    from lyra.pipeline import RunSummary

    assert "considered_paths" in {f.name for f in dataclasses.fields(RunSummary)}, (
        "RunSummary 必须记录实际处理过的曲目，写入才能与抓取对齐"
    )
    source = inspect.getsource(create_app)
    assert "s.considered_paths" in source, (
        "写入阶段必须使用抓取记录的曲目清单，否则 limit 仍只对抓取生效"
    )


def test_引用的枚举成员都真实存在():
    """`TransactionState.COMMITTED` 这种写错成员的引用要在测试期抓出来。

    ## 为什么需要

    实际写错过：`TransactionState.COMMITTED`（实际叫 `COMPLETED`）、
    `RunSummary.written` / `RunSummary.failed`。这类错误的共同点是
    **只在运行到那一行时才炸**，而那一行往往在「写入已经成功、正在记账」
    的位置——文件改好了，任务却报失败，非常难查。

    这里把源码里所有 `SomeClass.MEMBER` 形式的引用抽出来，
    对照该类真实存在的成员，写错立即报错。
    """
    import re
    from pathlib import Path

    import lyra.db as db

    # 只检查本项目自己的枚举/常量类，避免误判第三方库
    classes = {
        "TransactionState": db.TransactionState,
        "ResolutionOutcome": getattr(db, "ResolutionOutcome", None),
    }
    classes = {k: v for k, v in classes.items() if v is not None}

    unknown: list[str] = []
    for path in Path("src/lyra").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for cls_name, cls in classes.items():
            valid = {n for n in dir(cls) if not n.startswith("_")}
            for member in re.findall(rf"\b{cls_name}\.([A-Z][A-Z0-9_]*)\b", source):
                if member not in valid:
                    unknown.append(f"{path.name}: {cls_name}.{member}（实际有：{sorted(valid)}）")

    assert not unknown, "引用了不存在的枚举成员：\n  " + "\n  ".join(sorted(set(unknown)))


def test_摘要里的缺陷计数不含已缺失的位置(tmp_path, flac_factory):
    """筛选下拉说「缺 X（1）」，点进去却 0 条 —— 使用者会认为筛选坏了。

    缺失行的旧观察记录仍然在库里（标记缺失不删记录，这是**故意**的：
    文件可能只是临时移走）。但它们不该再出现在任何计数里。
    """
    from fastapi.testclient import TestClient

    from lyra.api import create_app
    from lyra.audit import audit_root
    from lyra.config import Config, RootConfig, StorageConfig, WebConfig
    from lyra.db import open_store
    from lyra.models import RootRole
    from lyra.pipeline import sync_state

    lib = tmp_path / "lib"
    flac_factory(lib / "a.flac", entries=(("TITLE", "A"), ("ARTIST", "X"), ("ALBUM", "Y")))
    flac_factory(lib / "b.flac", entries=(
        ("TITLE", "B"), ("ARTIST", "X"), ("ALBUM", "Y"),
        ("MUSICBRAINZ_TRACKID", "11111111-1111-1111-1111-111111111111"),
    ))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM lyric_observations WHERE issues LIKE '%missing_mbid%'"
    ).fetchone()[0] == 1

    # a.flac 被删掉（模拟用户移走/删除），整库核对后它被标成缺失
    (lib / "a.flac").unlink()
    sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]), full_scan=True)

    with TestClient(create_app(cfg)) as client:
        summary = client.get("/api/v1/summary").json()
        kinds = client.get("/api/v1/issue-kinds").json()
        tracks = client.get("/api/v1/tracks", params={"issue": "missing_mbid"}).json()

    assert summary["issues"].get("missing_mbid", 0) == 0, summary["issues"]
    codes = {k["code"] for k in kinds["kinds"]}
    assert "missing_mbid" not in codes, kinds
    assert tracks["total"] == 0, tracks
    store.close()


def test_统计里带上启用的来源(tmp_path, flac_factory):
    """诊断面板要能回答「到底接了哪几个源」。

    原来它读 `s.providers`，而 `/api/v1/stats` 根本不返回这个键——
    界面上「启用的来源」永远是「—」，看起来像配置没生效。
    """
    from fastapi.testclient import TestClient

    from lyra.api import create_app
    from lyra.config import Config, RootConfig, StorageConfig, WebConfig
    from lyra.models import RootRole

    lib = tmp_path / "lib"
    flac_factory(lib / "a.flac", entries=(("TITLE", "A"),))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        d = client.get("/api/v1/stats").json()
    names = [p["name"] for p in d.get("providers", [])]
    assert names == list(cfg.providers.enabled), names
    assert names, "至少要有一个来源，否则界面显示「—」"
