"""重构交付的后端能力：待办收件箱、任务明细、设置页、编辑器直写与入库流水线。

## 覆盖面

1. `GET /api/v1/todo` —— 六类待办的 counts + items（lyrics=staged needs_review、
   tags=tag_proposals），未知类别 400；
2. `GET /api/v1/runs/{task_id}` —— 任务行 + 每文件每环节的步骤矩阵，不存在 404；
3. `GET /api/v1/providers/health` —— 来源健康汇总（ok/blocked 计数与最后错误）；
4. `GET/PUT /api/v1/settings/{cover,organize,lyrics,acoustid}` —— 合法值生效、
   非法值 400 且不得部分生效；
5. `POST /api/v1/tracks/apply-lyrics` 的歌词编辑器模式：只给 text（不给来源）
   直接落盘；provider+text 同给时以来源正文为准、staged 记录来源；
6. `POST /api/v1/tasks/ingest`、`POST /api/v1/tasks/cover-fetch` —— 手动重跑
   入库流水线；以及 ingest.run_cover_stage / finish_new_files / find_sibling_cover
   的直接调用（同目录图嵌入、二次运行跳过、大小写不敏感探测）；
7. match 的近似匹配分级：版本冲突 → approximate，另一首歌 → 绝不近似。

## 网络边界（全部测试不联网）

- 批量支线：`create_providers` 打成空列表——`run_fetch` 在无来源时整段跳过；
- 单曲采用：`create_provider` 打成可控 FakeProvider；
- 封面：只走「同目录图」分支；标签里不带 MUSICBRAINZ_ALBUMID 时 CAA 分支
  查库无缓存后直接返回 None，不会发起下载。

## 等待后台任务的方式

外层任务行的落库在 `Runner.start` 里与作业完成之间存在竞态（start 的收尾
落库可能把 running 覆盖回去，见 api.Runner），所以这里不等外层行终态：

- 入库流水线**自己**的任务行（id 以 `ingest-` 开头）由作业线程顺序写入，
  没有竞态，以它为完成信号，再用 runs 接口断言明细；
- 补封面作业以文件实际效果（内嵌封面已出现）为完成信号。

每次轮询都新开一条 SQLite 连接，避免长连接停留在旧的 WAL 读快照上。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_flac, make_png_bytes
from fastapi.testclient import TestClient

import lyra.providers as providers_module
from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import (
    Config,
    PolicyConfig,
    ProvidersConfig,
    RootConfig,
    StorageConfig,
    WebConfig,
)
from lyra.db import open_store
from lyra.formats import read_audio_meta
from lyra.ingest import find_sibling_cover, finish_new_files, run_cover_stage
from lyra.match import build_context, decide, score_candidates
from lyra.models import RootRole
from lyra.pipeline import sync_state
from lyra.providers.base import Candidate, RawLyrics

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


class FakeProvider:
    """可控来源：只服务单曲采用接口（apply-lyrics）的取词路径。"""

    name = "qq"

    def __init__(self, lyrics_by_id: dict[str, RawLyrics]) -> None:
        self._lyrics = dict(lyrics_by_id)

    def search(self, _query: object) -> list[Candidate]:
        return [
            Candidate(provider="qq", provider_id=pid, title="盛夏光年",
                      artists=("五月天",), album="", duration=0.0)
            for pid in self._lyrics
        ]

    def song_detail(self, _song_id: str) -> dict[str, object]:
        return {"title": "盛夏光年", "artists": ["五月天"], "album": "",
                "duration": 0.0}

    def lyrics(self, candidate: Candidate) -> RawLyrics | None:
        return self._lyrics.get(candidate.provider_id)

    def close(self) -> None:
        pass


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """登录豁免的 API 客户端 + 临时曲库（扫描登记由各测试按需触发）。

    与 test_manual_lyrics_api 的同名夹具同一套构造方式；区别在于这里把
    来源列表置空（批量支线不联网），单曲路径用 FakeProvider。
    """
    lib = tmp_path / "lib"
    make_flac(lib / "专辑" / "歌.flac", entries=(
        ("TITLE", "盛夏光年"),
        ("ARTIST", "五月天"),
    ))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
        # 批量支线（ingest）不构造任何来源：run_fetch 在 providers 为空时整段跳过
        providers=ProvidersConfig(enabled=[]),
    )

    def resync() -> None:
        with _store(cfg) as store:
            sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))

    good = FakeProvider({
        "src1": RawLyrics(
            provider="qq", provider_id="src1",
            text="[00:01.00]来自来源的正文\n[00:02.00]第二行\n", synced=True),
    })

    def fake_create_provider(name: str, _config: object, **_kw: object):
        if name == "qq":
            return good
        raise ValueError(f"未知来源 {name!r}")

    monkeypatch.setattr(
        providers_module, "create_providers", lambda _config, **_kw: [])
    monkeypatch.setattr(providers_module, "create_provider", fake_create_provider)

    app = create_app(cfg)
    with TestClient(app) as client:
        client.cfg = cfg            # type: ignore[attr-defined]
        client.lib = lib            # type: ignore[attr-defined]
        client.db_path = cfg.storage.resolved_data_dir / "lyra.db"  # type: ignore[attr-defined]
        client.resync = resync      # type: ignore[attr-defined]
        client.provider = good      # type: ignore[attr-defined]
        yield client


@contextmanager
def _store(cfg: Config) -> Iterator:
    """独立打开一个状态库连接，用完即关（与 API 自己的连接互不干扰）。"""
    handle = open_store(cfg.storage.resolved_data_dir)
    try:
        yield handle
    finally:
        handle.close()


def _fresh_query(db_path: Path, sql: str, params: tuple = ()) -> list:
    """每次轮询都新开一条连接读库：绝不依赖任何长连接的 WAL 快照。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _wait_ingest_task_done(db_path: Path, *, timeout: float = 20.0) -> str:
    """等入库流水线自己的任务行到达 done，返回任务 id。

    外层任务行由 Runner.start 落库，与作业完成之间存在竞态；入库流水线
    自己的任务行（id 以 ingest- 开头）由作业线程顺序写入，没有竞态。
    """
    deadline = time.monotonic() + timeout
    last = "没有找到任务行"
    while time.monotonic() < deadline:
        rows = _fresh_query(
            db_path,
            "SELECT id, state FROM tasks WHERE kind='ingest' AND id LIKE 'ingest-%' "
            "ORDER BY started_at DESC LIMIT 1",
        )
        if rows:
            last = str(rows[0]["state"])
            if last == "done":
                return str(rows[0]["id"])
        time.sleep(0.02)
    raise AssertionError(f"入库流水线任务没有在 {timeout}s 内完成（最后状态：{last}）")


def _wait_cover_embedded(audio: Path, base: Path, *, timeout: float = 20.0) -> None:
    """等补封面作业把封面真正写进音频（作业体完成的可靠信号）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_audio_meta(audio, base).embedded.has_cover:
            return
        time.sleep(0.02)
    raise AssertionError(f"补封面任务没有在 {timeout}s 内把封面写进 {audio.name}")


def _location_id(store, rel_path: str) -> int:
    """取已登记位置的主键（调用前需先 env.resync() 扫描登记）。"""
    root_id = store.get_root_id("lib")
    assert root_id is not None, "受管根 lib 应已登记"
    loc = store.location_by_rel_path(root_id, rel_path)
    assert loc is not None, f"位置 {rel_path} 应已登记"
    return int(loc["id"])


# ---------------------------------------------------------------------------
# 1. 待办收件箱 GET /api/v1/todo
# ---------------------------------------------------------------------------


def test_todo_空库六类计数全为零(env):
    r = env.get("/api/v1/todo")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"] == {
        "lyrics": 0, "tags": 0, "covers": 0,
        "organize": 0, "orphans": 0, "failed": 0,
    }
    assert d["items"] == []
    assert d["total"] == 0


def test_todo_待审歌词进入lyrics队列(env):
    env.resync()
    with _store(env.cfg) as store:
        staged_id = store.stage_lyrics(
            location_id=_location_id(store, "专辑/歌.flac"),
            content_hash="hash-lyrics-1",
            content="[00:01.00]待人工复核的词\n",
            provider="qq", provider_id="1",
            state="needs_review", origin="auto",
        )

    r = env.get("/api/v1/todo", params={"kind": "lyrics"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"]["lyrics"] == 1, d["counts"]
    assert d["total"] == 1
    assert len(d["items"]) == 1
    item = d["items"][0]
    assert item["ref"] == f"staged:{staged_id}"
    assert item["root"] == "lib"
    assert item["rel_path"] == "专辑/歌.flac"
    assert item["provider"] == "qq"
    assert item["state"] == "needs_review"


def test_todo_标签提案进入tags队列(env):
    env.resync()
    with _store(env.cfg) as store:
        proposal_id = store.upsert_tag_proposal(
            location_id=_location_id(store, "专辑/歌.flac"),
            state="review",
            source="musicbrainz",
            confidence=0.92,
            title="盛夏光年",
            artists="五月天",
            album="追凶二十年 电影原声带",
            payload={"title": "盛夏光年", "artists": ["五月天"]},
        )

    r = env.get("/api/v1/todo", params={"kind": "tags"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"]["tags"] == 1, d["counts"]
    assert len(d["items"]) == 1
    item = d["items"][0]
    assert item["ref"] == f"proposal:{proposal_id}"
    assert item["rel_path"] == "专辑/歌.flac"
    assert (item["title"], item["artists"], item["state"], item["source"]) == (
        "盛夏光年", "五月天", "review", "musicbrainz")


def test_todo_未知类别返回400(env):
    r = env.get("/api/v1/todo", params={"kind": "bogus"})
    assert r.status_code == 400, r.text
    assert "未知待办类别" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 任务明细 GET /api/v1/runs/{task_id}
# ---------------------------------------------------------------------------


def test_runs_返回任务行与步骤矩阵(env):
    with _store(env.cfg) as store:
        store.upsert_task(
            task_id="task-demo", kind="ingest", state="done",
            progress="2/2", result={"fetched": 1},
            started_at=time.time() - 10, finished_at=time.time(),
        )
        store.add_task_step(task_id="task-demo", seq=1, kind="lyrics",
                            rel_path="专辑/歌.flac", state="done",
                            detail={"accepted": 1})
        store.add_task_step(task_id="task-demo", seq=2, kind="cover",
                            rel_path="专辑/歌.flac", state="review",
                            detail={"note": "缺封面"})

    r = env.get("/api/v1/runs/task-demo")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["task"]["id"] == "task-demo"
    assert d["task"]["kind"] == "ingest"
    assert d["task"]["state"] == "done"
    assert d["task"]["result"] == {"fetched": 1}
    assert d["total_steps"] == 2
    assert [s["seq"] for s in d["steps"]] == [1, 2]
    assert d["steps"][0]["kind"] == "lyrics"
    assert d["steps"][0]["rel_path"] == "专辑/歌.flac"
    assert d["steps"][0]["detail"] == {"accepted": 1}
    assert d["steps"][1]["state"] == "review"


def test_runs_不存在的任务返回404(env):
    r = env.get("/api/v1/runs/no-such-task")
    assert r.status_code == 404, r.text
    assert "任务不存在" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 来源健康 GET /api/v1/providers/health
# ---------------------------------------------------------------------------


def test_来源健康汇总包含ok与blocked计数(env):
    with _store(env.cfg) as store:
        store.bump_provider_health("qq", ok=True)
        store.bump_provider_health("qq", ok=True)
        store.bump_provider_health("lrclib", blocked=True, error="上游风控 412")

    r = env.get("/api/v1/providers/health")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["days"] == 30
    by_name = {p["provider"]: p for p in d["providers"]}
    assert by_name["qq"]["ok"] == 2, by_name
    assert by_name["lrclib"]["blocked"] == 1, by_name
    assert "风控" in by_name["lrclib"]["last_error"]


# ---------------------------------------------------------------------------
# 4. 设置页 GET/PUT /api/v1/settings/*
# ---------------------------------------------------------------------------


def test_封面设置合法值生效非法值被拒(env):
    r = env.put("/api/v1/settings/cover", json={"auto_write": True})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["auto_write"] is True
    assert env.get("/api/v1/settings/cover").json()["policy"]["auto_write"] is True

    r = env.put("/api/v1/settings/cover", json={"auto_write": "yes"})
    assert r.status_code == 400, r.text
    # 非法提交不得部分生效
    assert env.get("/api/v1/settings/cover").json()["policy"]["auto_write"] is True


def test_归置设置模式只认白名单(env):
    r = env.put("/api/v1/settings/organize", json={"mode": "new_files"})
    assert r.status_code == 200, r.text
    assert r.json()["policy"]["mode"] == "new_files"
    assert env.get("/api/v1/settings/organize").json()["policy"]["mode"] == "new_files"

    r = env.put("/api/v1/settings/organize", json={"mode": "bogus"})
    assert r.status_code == 400, r.text
    assert "mode" in r.json()["detail"]
    assert env.get("/api/v1/settings/organize").json()["policy"]["mode"] == "new_files"


def test_歌词设置翻译模式含布尔旧写法(env):
    r = env.put("/api/v1/settings/lyrics", json={"translation": "bilingual"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["translation"] == "bilingual"
    got = env.get("/api/v1/settings/lyrics").json()["settings"]["translation"]
    assert got == "bilingual"

    # 旧配置的布尔写法：true → bilingual
    r = env.put("/api/v1/settings/lyrics", json={"translation": True})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["translation"] == "bilingual"

    r = env.put("/api/v1/settings/lyrics", json={"translation": "nope"})
    assert r.status_code == 400, r.text
    got = env.get("/api/v1/settings/lyrics").json()["settings"]["translation"]
    assert got == "bilingual", "非法值不得改掉已生效的设置"


def test_acoustid设置保存密钥并掩码显示(env, monkeypatch):
    monkeypatch.delenv("LYRA_ACOUSTID_API_KEY", raising=False)

    r = env.put("/api/v1/settings/acoustid",
                json={"api_key": "abcd1234efgh", "enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["has_key"] is True

    r = env.get("/api/v1/settings/acoustid")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enabled"] is True
    assert d["has_key"] is True
    assert d["masked"] == "abcd…efgh", d
    assert d["source"] == "settings"


# ---------------------------------------------------------------------------
# 5. 歌词编辑器直写 POST /api/v1/tracks/apply-lyrics
# ---------------------------------------------------------------------------


def test_编辑器模式只给文本即可写入(env):
    """歌词编辑器没有来源可言：人只改一两行，编辑文本直接落盘。"""
    env.resync()
    text = "[00:01.00]编辑器改的第一行\n[00:02.00]编辑器改的第二行\n"
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac", "text": text, "apply": True,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"]["ok"] is True, d
    assert d["applied"]["written"] == 1

    sidecar = env.lib / "专辑" / "歌.lrc"
    assert sidecar.is_file(), "编辑文本应写出同名 .lrc"
    written = sidecar.read_text(encoding="utf-8")
    assert "编辑器改的第一行" in written
    assert "编辑器改的第二行" in written

    # 编辑器没有来源：staged 记录为 manual/editor，而非任何抓取来源
    with _store(env.cfg) as store:
        row = store.conn.execute(
            "SELECT provider, provider_id, origin FROM staged_lyrics WHERE id=?",
            (d["staged_id"],),
        ).fetchone()
    assert row is not None
    assert (row["provider"], row["provider_id"], row["origin"]) == (
        "manual", "editor", "manual")


def test_编辑器模式既无文本也无来源时返回400(env):
    env.resync()
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac"})
    assert r.status_code == 400, r.text
    assert "编辑文本" in r.json()["detail"]


def test_来源与文本同给时落盘来源正文且staged记来源(env):
    """text 与 provider 同给时 text 一律忽略：接口不能成为伪造来源正文的入口。"""
    env.resync()
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "src1",
        "text": "[00:09.00]客户端伪造的正文",
        "apply": True,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"]["ok"] is True, d

    written = (env.lib / "专辑" / "歌.lrc").read_text(encoding="utf-8")
    assert "来自来源的正文" in written
    assert "客户端伪造的正文" not in written

    with _store(env.cfg) as store:
        row = store.conn.execute(
            "SELECT provider, provider_id FROM staged_lyrics WHERE id=?",
            (d["staged_id"],),
        ).fetchone()
    assert row is not None
    assert (row["provider"], row["provider_id"]) == ("qq", "src1")


# ---------------------------------------------------------------------------
# 6. 手动任务 POST /api/v1/tasks/ingest 与 /api/v1/tasks/cover-fetch
# ---------------------------------------------------------------------------


def test_手动重跑入库流水线整条跑通(env):
    r = env.post("/api/v1/tasks/ingest", json={"root": "lib"})
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["kind"] == "ingest"

    # 等流水线**自己**的任务行到 done（没有落库竞态），再经 runs 接口看明细
    inner_id = _wait_ingest_task_done(env.db_path)

    detail = env.get(f"/api/v1/runs/{inner_id}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["task"]["kind"] == "ingest"
    assert d["task"]["state"] == "done"
    assert d["total_steps"] >= 3, d
    lyrics_steps = [s for s in d["steps"] if s["kind"] == "lyrics"]
    assert lyrics_steps, d
    # 来源列表为空时歌词支线整段跳过，而不是报错
    assert lyrics_steps[0]["state"] == "skipped"
    assert lyrics_steps[0]["detail"]["why"] == "无可用来源"

    # 外层任务行也能经 runs 接口查到
    outer = env.get(f"/api/v1/runs/{started['id']}")
    assert outer.status_code == 200, outer.text
    assert outer.json()["task"]["kind"] == "ingest"


def test_手动补封面嵌入同目录图(env):
    audio = env.lib / "专辑" / "歌.flac"
    assert not read_audio_meta(audio, env.lib).embedded.has_cover
    (env.lib / "专辑" / "cover.png").write_bytes(make_png_bytes())

    r = env.post("/api/v1/tasks/cover-fetch", json={"root": "lib"})
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["kind"] == "cover-fetch"

    # 以文件实际效果为完成信号（外层任务行的终态落库有已知竞态，不作依据）
    _wait_cover_embedded(audio, env.lib)

    outer = env.get(f"/api/v1/runs/{started['id']}")
    assert outer.status_code == 200, outer.text
    assert outer.json()["task"]["kind"] == "cover-fetch"


# ---------------------------------------------------------------------------
# 7. 入库支线的直接调用（ingest.run_cover_stage / finish_new_files）
# ---------------------------------------------------------------------------


def test_封面支线嵌入同目录图且二次运行跳过(env):
    (env.lib / "新专辑").mkdir()
    make_flac(env.lib / "新专辑" / "新歌.flac")  # 无标签：不带 MBID，CAA 不联网
    (env.lib / "新专辑" / "cover.png").write_bytes(make_png_bytes())

    with _store(env.cfg) as store:
        result = run_cover_stage(
            store, env.cfg, env.cfg.roots[0], rel_paths=["新专辑/新歌.flac"])

    assert result["embedded"] == 1, result
    assert result["steps"] == [
        ("新专辑/新歌.flac", "done", {"source": "sibling", "file": "cover.png"}),
    ], result

    meta = read_audio_meta(env.lib / "新专辑" / "新歌.flac", env.lib)
    assert meta.embedded.has_cover

    with _store(env.cfg) as store:
        again = run_cover_stage(
            store, env.cfg, env.cfg.roots[0], rel_paths=["新专辑/新歌.flac"])
    assert again["embedded"] == 0, again
    assert again["steps"] == [
        ("新专辑/新歌.flac", "skipped", {"why": "已有内嵌封面"}),
    ], again


def test_finish_new_files只补封面与归位不再抓词(env):
    """watch 主循环的后半程：审计与抓词已由调用方完成，这里只补封面与归位。"""
    (env.lib / "watch入库").mkdir()
    make_flac(env.lib / "watch入库" / "后半程.flac")
    (env.lib / "watch入库" / "front.PNG").write_bytes(make_png_bytes())

    with _store(env.cfg) as store:
        out = finish_new_files(
            store, env.cfg, env.cfg.roots[0],
            ["watch入库/后半程.flac"], task_id="watch-half",
        )

    # organize mode 默认 off：只出建议不动文件，所以 moved 为 0
    assert out == {"covers": 1, "moved": 0}, out
    meta = read_audio_meta(env.lib / "watch入库" / "后半程.flac", env.lib)
    assert meta.embedded.has_cover


# ---------------------------------------------------------------------------
# 8. 同目录封面探测（ingest.find_sibling_cover）
# ---------------------------------------------------------------------------


def test_同目录封面探测大小写不敏感(tmp_path):
    audio = make_flac(tmp_path / "大小写" / "歌.flac")
    (tmp_path / "大小写" / "Cover.JPG").write_bytes(make_png_bytes())
    hit = find_sibling_cover(audio)
    assert hit is not None, "大写扩展名的规范命名封面必须能命中"
    assert hit.name == "Cover.JPG"


def test_同目录封面探测不认无关文件(tmp_path):
    audio = make_flac(tmp_path / "无关文件" / "歌.flac")
    (tmp_path / "无关文件" / "unrelated.txt").write_text("不是图片", encoding="utf-8")
    assert find_sibling_cover(audio) is None


# ---------------------------------------------------------------------------
# 9. 匹配近似分级（match：decision.approximate / verdict='approximate'）
# ---------------------------------------------------------------------------

_MATCHING_POLICY = PolicyConfig().matching


def _cand(title: str, artist: str, duration: float) -> SimpleNamespace:
    return SimpleNamespace(provider="qq", provider_id="x", title=title,
                           artists=(artist,), album="", duration=duration)


def _run_match(tags: dict, duration: float, candidates: list):
    ctx = build_context(tags, duration)
    return decide(ctx, score_candidates(ctx, candidates, _MATCHING_POLICY),
                  _MATCHING_POLICY)


def test_版本冲突时给近似匹配分级():
    """本地「喜欢你 (Live)」配到录音室版「喜欢你」：同一首歌的不同版本。

    决策仍是 needs_review（版本闸门未过，绝不自动写），但分级为
    approximate——歌词内容大概率适用，人工确认即可采用。
    """
    result = _run_match(
        {"TITLE": ["喜欢你 (Live)"], "ARTIST": ["G.E.M. 鄧紫棋"]},
        262.0,
        [_cand("喜欢你", "G.E.M.邓紫棋", 262.0)],
    )
    assert result.decision.value == "needs_review"
    assert result.approximate is True
    assert result.recommendation is not None
    assert result.recommendation.verdict == "approximate"


def test_另一首歌绝不进近似匹配():
    """标题核心不达标就是另一首歌：近似通道宁缺勿滥。"""
    result = _run_match(
        {"TITLE": ["晴天"], "ARTIST": ["周杰伦"]},
        269.3,
        [_cand("放弃", "周杰伦", 269.3)],
    )
    assert result.decision.value == "needs_review"
    assert result.approximate is False
