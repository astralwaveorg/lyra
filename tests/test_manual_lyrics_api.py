"""单曲手工选歌词：三个接口的行为边界。

## 为什么必须有

音乐库页的「刮削歌词」原来是黑箱：点一下、后台跑、结果好坏都看不到。
改成「列候选 → 预览 → 选用并写入」之后，引入的新风险有两类：

1. **越权写入**。这一组接口按路径操作文件，是继预览与删除之后第三处
   接受用户指定路径的地方；`apply-lyrics` 还能写文件。路径校验必须与
   删除/替换同一套（拒绝绝对路径、`..`、符号链接、根外目标）。
2. **把接口变成任意内容写入入口**。接口只接受 `provider` + `provider_id`，
   正文一律由服务端重新向来源取回。测试里专门提交一份伪造正文，断言
   落盘的是来源的内容而不是客户端提交的内容。

另外固定两条语义：
- 预览只对**被点中的那一条**取歌词（否则「列 8 条候选」会变成 8 次请求）；
- 只有 `replace_existing=true` 才允许覆盖文件里已有的歌词。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lyra.providers as providers_module
from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import sync_state
from lyra.providers.base import Candidate, ProviderError, RawLyrics


class FakeProvider:
    """一个可控的来源：候选与歌词都由测试给定。"""

    name = "qq"

    def __init__(self, candidates, lyrics_by_id, calls):
        self._candidates = list(candidates)
        self._lyrics = dict(lyrics_by_id)
        self._calls = calls

    def search(self, query):
        self._calls["search"] = self._calls.get("search", 0) + 1
        return list(self._candidates)

    def song_detail(self, song_id):
        self._calls.setdefault("detail", []).append(song_id)
        return {"title": "存在 (Live)", "artists": ["G.E.M.邓紫棋"],
                "album": "我是歌手第二季 第2期", "duration": 247.0}

    def lyrics(self, candidate):
        self._calls.setdefault("lyrics", []).append(candidate.provider_id)
        return self._lyrics.get(candidate.provider_id)

    def close(self):
        pass


class BoomProvider(FakeProvider):
    name = "lrclib"

    def search(self, query):
        raise ProviderError("lrclib", "上游 500")


def _cand(pid, title, artists, duration):
    return Candidate(
        provider="qq", provider_id=pid, title=title,
        artists=tuple(artists), album="", duration=duration,
    )


@pytest.fixture
def env(tmp_path: Path, flac_factory, monkeypatch):
    lib = tmp_path / "lib"
    flac_factory(lib / "专辑" / "歌.flac", entries=(
        ("TITLE", "盛夏光年"),
        ("ARTIST", "五月天"),
    ))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )

    # 登记 location：apply-lyrics 要求这个文件已经被扫描过
    def resync():
        store = open_store(cfg.storage.resolved_data_dir)
        try:
            sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
        finally:
            store.close()

    resync()

    calls: dict = {}
    good = FakeProvider(
        candidates=[_cand("1", "盛夏光年", ["五月天"], 0.0 + _DURATION)],
        lyrics_by_id={
            "1": RawLyrics(
                provider="qq", provider_id="1",
                text="[00:01.00]来自来源的正文\n[00:02.00]第二行\n", synced=True),
            # 人工链接指定的那一条：它**有意不在**上面的搜索结果里，
            # 这正是「按 id 直接取词」要覆盖的情形。
            _LINKED_ID: RawLyrics(
                provider="qq", provider_id=_LINKED_ID,
                text="[00:01.00]来自来源的正文\n[00:02.00]第二行\n", synced=True),
            # 末行落在音频结束之后：完整版的词配到了短版文件上。
            "2": RawLyrics(
                provider="qq", provider_id="2",
                text="[00:01.00]来自来源的正文\n[09:00.00]末行超出音频\n",
                synced=True),
        },
        calls=calls,
    )
    bad = BoomProvider([], {}, calls)

    def fake_create_providers(_config, **_kw):
        return [good, bad]

    def fake_create_provider(name, _config, **_kw):
        if name == "qq":
            return good
        if name == "lrclib":
            return bad
        raise ValueError(f"未知来源 {name!r}")

    monkeypatch.setattr(providers_module, "create_providers", fake_create_providers)
    monkeypatch.setattr(providers_module, "create_provider", fake_create_provider)

    app = create_app(cfg)
    with TestClient(app) as c:
        c.cfg = cfg            # type: ignore[attr-defined]
        c.lib = lib            # type: ignore[attr-defined]
        c.calls = calls        # type: ignore[attr-defined]
        c.resync = resync      # type: ignore[attr-defined]
        c.lyrics_by_id = good._lyrics  # type: ignore[attr-defined]
        yield c


# 合成 FLAC 的 STREAMINFO 给出固定时长；候选时长必须与之一致才能过时长闸门。
# 由 flac_factory 的 build_streaminfo 决定，这里从实际文件读一次更可靠。
@pytest.fixture(autouse=True)
def _resolve_duration(env):
    from lyra.formats import read_audio_meta

    base = env.lib
    meta = read_audio_meta(base / "专辑" / "歌.flac", base)
    global _DURATION
    _DURATION = meta.info.duration or 0.0


_DURATION = 0.0

#: 人工链接指定的歌曲 id（有意不在搜索结果里）
_LINKED_ID = "002APLOP0C614e"


def test_路径越界一律被拒(env):
    """这一组接口同样是「按路径读/写」，边界必须和删除一致。"""
    for bad in ("../secret.flac", "/etc/passwd", "专辑/../../x.flac"):
        r = env.get("/api/v1/tracks/candidates", params={"root": "lib", "path": bad})
        assert r.status_code in (400, 404), f"{bad} 竟然通过了：{r.status_code}"


def test_候选接口列出各来源并标记系统选择(env):
    r = env.get("/api/v1/tracks/candidates", params={"root": "lib", "path": "专辑/歌.flac"})
    assert r.status_code == 200, r.text
    d = r.json()

    assert [s["provider"] for s in d["sources"]] == ["qq", "lrclib"]
    # 失败的来源必须逐源报出来，而不是整体变成「没有候选」
    lrclib = next(s for s in d["sources"] if s["provider"] == "lrclib")
    assert lrclib["error"] and "500" in lrclib["error"]

    assert len(d["options"]) == 1
    opt = d["options"][0]
    assert opt["provider"] == "qq" and opt["provider_id"] == "1"
    assert opt["is_best"] is True
    assert opt["is_chosen"] is True, "唯一候选且证据齐全时应被标成「系统会选这条」"
    # 逐闸门判定必须带上，界面才能说清「为什么没自动接受」
    assert {g["key"] for g in opt["gates"]} >= {"duration_gate", "title", "artist"}
    assert d["verdict"]["decision"] in ("accept", "needs_review", "no_match")
    # 列候选**不应该**取歌词（否则 8 条候选就是 8 次请求）
    assert "lyrics" not in env.calls


def test_预览只取被点中的那一条(env):
    r = env.get("/api/v1/tracks/lyrics-preview", params={
        "root": "lib", "path": "专辑/歌.flac", "provider": "qq", "provider_id": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "来自来源的正文" in d["text"]
    assert d["synced"] is True and d["timestamp_count"] == 2
    assert env.calls.get("lyrics") == ["1"], "只应取回被点中的那一条"


def test_预览候选不存在时给出可重试的说明(env):
    r = env.get("/api/v1/tracks/lyrics-preview", params={
        "root": "lib", "path": "专辑/歌.flac", "provider": "qq", "provider_id": "999"})
    assert r.status_code == 404
    assert "重新查询候选" in r.json()["detail"]


def test_预览说明时间轴有没有超出音频(env):
    """时间轴超出音频长度时必须看得出来。

    同一首歌的短版常常配着完整版的时间轴：内容对得上，末行却落在
    音频结束之后。界面要在采用之前就摆出来——否则只会在写入后
    看到一句「时间轴异常」，使用者根本不知道该换哪一条。
    """
    r = env.get("/api/v1/tracks/lyrics-preview", params={
        "root": "lib", "path": "专辑/歌.flac", "provider": "qq", "provider_id": "2",
        "by_id": "true"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["duration_seconds"], d
    assert d["last_timestamp_ms"] == 540_000, d
    assert d["overrun_seconds"] > 500, d

    # 正常的那一条不该被误报
    r2 = env.get("/api/v1/tracks/lyrics-preview", params={
        "root": "lib", "path": "专辑/歌.flac", "provider": "qq", "provider_id": "1"})
    d2 = r2.json()
    assert d2["overrun_seconds"] == 0, d2


def test_采用并写入会落盘且用来源正文(env):
    """接口**只接受来源与候选 id**，正文由服务端重新取回。

    这里故意提交一份伪造正文，断言落盘的是来源的那份——
    否则这个接口就成了「往音乐库里写任意内容」的入口。
    """
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1",
        "apply": True,
        "text": "[00:09.00]客户端伪造的正文",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"]["ok"] is True, d
    assert d["applied"]["written"] == 1

    sidecar = env.lib / "专辑" / "歌.lrc"
    assert sidecar.is_file(), "应写出同名 .lrc"
    written = sidecar.read_text(encoding="utf-8")
    assert "来自来源的正文" in written
    assert "客户端伪造的正文" not in written


def test_已有歌词时默认拒绝覆盖(env):
    """文件里已经有歌词时，服务端要拦住，不能只靠界面上的勾选框。

    覆盖别人的歌词虽然可恢复（有备份），但代价是整份文件复制一次；
    更重要的是「界面勾选框」这种东西是可以被绕过的，服务端不拦就等于没拦。
    """
    sidecar = env.lib / "专辑" / "歌.lrc"
    sidecar.write_text("[00:01.00]我自己写的歌词\n", encoding="utf-8")
    env.resync()

    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": True})
    assert r.status_code == 409, r.text
    assert "覆盖" in r.json()["detail"]
    assert "我自己写的歌词" in sidecar.read_text(encoding="utf-8")


def test_明确要求覆盖时才替换(env):
    sidecar = env.lib / "专辑" / "歌.lrc"
    sidecar.write_text("[00:01.00]我自己写的歌词\n", encoding="utf-8")
    env.resync()

    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1",
        "apply": True, "replace_existing": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"]["written"] == 1, d
    written = sidecar.read_text(encoding="utf-8")
    assert "来自来源的正文" in written
    assert "我自己写的歌词" not in written


def test_仅暂存时不写文件(env):
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": False})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is None
    assert not (env.lib / "专辑" / "歌.lrc").exists()


# ---------------------------------------------------------------------------
# 人工选定的歌词必须真的写进去
# ---------------------------------------------------------------------------


def test_纯文本歌词也能写入(env):
    """人工选的那条**没有时间轴**时也必须写进去。

    实际故障：网易云与 LRCLIB 的很多曲目只有纯文本歌词。自动流程拒绝它们
    是对的（纯文本会被当成垃圾清掉，写了也白写），但人工在界面上逐条看过
    之后选定了一条，那就是决定。旧实现里复核只认 synced，人工选的纯文本
    歌词被判定「写入后内嵌歌词状态异常: plain_text」，**回滚并报错**——
    实际生产里两次人工写入都这样失败。
    """
    env.calls.setdefault("lyrics", [])
    env.lyrics_by_id["plain"] = RawLyrics(
        provider="qq", provider_id="plain",
        text="第一行纯文本\n第二行纯文本\n", synced=False,
    )

    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "plain",
        "apply": True, "by_id": True})
    assert r.status_code == 200, r.text
    applied = r.json()["applied"]
    assert applied["ok"] is True, applied
    assert applied["written"] == 1, applied

    sidecar = env.lib / "专辑" / "歌.lrc"
    assert sidecar.is_file()
    assert "第一行纯文本" in sidecar.read_text(encoding="utf-8")


def test_只写入人工选中的那一条(env):
    """同一位置可能留着历史抓取的其它待写入行，人工只认他刚选中的这条。

    旧实现把该位置**全部**待写入行都写一遍：白写一次（还多一次备份），
    最终内容取决于迭代顺序——那是不可接受的随机性。
    """
    store = open_store(env.cfg.storage.resolved_data_dir)
    try:
        location = store.conn.execute(
            "SELECT id FROM locations WHERE rel_path=?", ("专辑/歌.flac",)
        ).fetchone()
        assert location is not None
        store.stage_lyrics(
            location_id=int(location["id"]),
            content_hash="deadbeef",
            content="[00:03.00]旧的那一条\n",
            provider="qq", provider_id="old", state="staged", origin="auto",
        )
    finally:
        store.close()

    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1",
        "apply": True})
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["written"] == 1, r.json()

    written = (env.lib / "专辑" / "歌.lrc").read_text(encoding="utf-8")
    assert "来自来源的正文" in written
    assert "旧的那一条" not in written

    # 旧的那一条必须被作废，否则下一轮批量写入会把它写回去
    store = open_store(env.cfg.storage.resolved_data_dir)
    try:
        states = {
            row["id"]: row["state"]
            for row in store.conn.execute(
                "SELECT id, state FROM staged_lyrics WHERE location_id=?",
                (int(location["id"]),),
            )
        }
    finally:
        store.close()
    assert "superseded" in states.values(), states


def test_重复采用同一份歌词照样写入(env):
    """文件里已经是这份歌词时，再点一次「采用并写入」要真的写进去。

    旧实现把「内容一样」当成「无需重复写入」，使用者看着像写入失败。
    而内容一样**不代表文件是对的**：同一首歌的短版配了完整版的时间轴
    （末行超出音频几十秒）就是这种情况——内容指纹相同，时间轴却是错的。
    人工点了「采用并写入」，要的就是落盘。
    """
    payload = {
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": True,
    }
    first = env.post("/api/v1/tracks/apply-lyrics", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["applied"]["ok"] is True

    second = env.post("/api/v1/tracks/apply-lyrics", json={**payload, "replace_existing": True})
    assert second.status_code == 200, second.text
    applied = second.json()["applied"]
    assert applied["ok"] is True, applied
    # 这一次是真的写了进去，而不是「无需重复写入」
    assert applied["written"] == 1, applied
    assert applied["satisfied"] == 0, applied
    # 回执要说清楚「写了，但内容本来一样」，不能让人以为白改了
    assert "已强制写入" in applied["message"], applied


def test_显式关闭强制时内容相同就跳过(env):
    """不想重写时（force_rewrite=false）保留旧行为：内容一样就不写。"""
    payload = {
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": True,
    }
    first = env.post("/api/v1/tracks/apply-lyrics", json=payload)
    assert first.status_code == 200, first.text

    second = env.post(
        "/api/v1/tracks/apply-lyrics",
        json={**payload, "replace_existing": True, "force_rewrite": False},
    )
    assert second.status_code == 200, second.text
    applied = second.json()["applied"]
    assert applied["ok"] is True, applied
    assert applied["written"] == 0, applied
    assert applied["satisfied"] == 1, applied
    assert "已经是" in applied["message"]


def test_写入失败时给出可读的原因(env, monkeypatch):
    """没有真正写进去时必须说清为什么，不能只丢一个「写入失败」。"""
    import lyra.pipeline as pipeline_module
    from lyra.writer import WriteOutcome

    def boom(*_args, **_kwargs):
        return WriteOutcome(
            ok=False, txn_id="", state="failed",
            error="UnsupportedStructure: 写入后复核未通过: 内部错误",
        )

    monkeypatch.setattr(pipeline_module, "apply_plan", boom)

    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": True})
    assert r.status_code == 200, r.text
    applied = r.json()["applied"]
    assert applied["ok"] is False
    assert "复核未通过" in applied["error"], applied
    assert applied["errors"], applied


# ---------------------------------------------------------------------------
# 按链接取词（人工指定）
# ---------------------------------------------------------------------------


def test_按链接取词直接用链接里的歌曲_id(env):
    """链接是「我确定是这一首」的人工断言，**不跑匹配闸门**。

    实际动机：`06 G.E.M. 鄧紫棋 - 存在.flac` 在 QQ 上同名歌曲有 20 多条不同
    录音，自动匹配只能按标题+时长猜，猜出来的全是别人的版本。使用者自己
    知道是哪一首，贴一个链接应当一次到位。
    """
    r = env.post("/api/v1/tracks/resolve-link", json={
        "root": "lib", "path": "专辑/歌.flac",
        "url": "https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["provider"], d["provider_id"]) == ("qq", "002APLOP0C614e")
    assert "来自来源的正文" in d["text"]
    assert d["synced"] is True and d["timestamp_count"] == 2
    # 详情用于让使用者确认「你给的这一首到底是什么」
    assert d["detail"]["title"] == "存在 (Live)"
    assert d["detail"]["artists"] == ["G.E.M.邓紫棋"]
    # 取词必须用链接里的 id，而不是搜索出来的候选
    assert env.calls.get("lyrics") == ["002APLOP0C614e"]
    assert env.calls.get("search") is None, "按链接取词不该再走搜索"


def test_无法识别的链接给出可照做的错误(env):
    r = env.post("/api/v1/tracks/resolve-link", json={
        "root": "lib", "path": "专辑/歌.flac", "url": "https://example.com/x"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "y.qq.com" in detail and "music.163.com" in detail


def test_按链接取词同样受路径边界约束(env):
    r = env.post("/api/v1/tracks/resolve-link", json={
        "root": "lib", "path": "../secret.flac",
        "url": "https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e"})
    assert r.status_code in (400, 404)


def test_按链接取到的歌词可以被采用并写入(env):
    """链接候选走的是与普通候选**同一个**采用接口，不能有第二条写入路径。"""
    r = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": _LINKED_ID,
        "apply": True, "by_id": True})
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["ok"] is True
    assert "来自来源的正文" in (env.lib / "专辑" / "歌.lrc").read_text(encoding="utf-8")


def test_候选接口要告诉界面这个文件已经有歌词(env):
    """界面曾自己按 `embedded_state` 判断，漏掉 `.lrc`。

    结果：文件只有外挂歌词时，对话框说「该文件当前无歌词」、不渲染
    「覆盖已有歌词」勾选框；点「采用并写入」被服务端 409 挡住，
    而报错让使用者去勾一个**界面上不存在**的勾选框——死路。
    实际 01 Mojito.flac 就是这样（有 .lrc、无内嵌）。

    所以「是否已有歌词」必须由**服务端**给（读审计观察，内嵌与 .lrc 同一套
    语义），界面不再自己猜。
    """
    path = {"root": "lib", "path": "专辑/歌.flac"}

    first = env.get("/api/v1/tracks/candidates", params=path).json()
    assert first["has_existing_lyrics"] is False

    (env.lib / "专辑" / "歌.lrc").write_text("[00:01.00]只有外挂歌词\n", encoding="utf-8")
    env.resync()

    second = env.get("/api/v1/tracks/candidates", params=path).json()
    assert second["has_existing_lyrics"] is True, "只有 .lrc 也必须算「已有歌词」"

    # 与写入接口的判定一致：默认拒绝覆盖，明确勾选才写
    blocked = env.post("/api/v1/tracks/apply-lyrics", json={
        "root": "lib", "path": "专辑/歌.flac",
        "provider": "qq", "provider_id": "1", "apply": True})
    assert blocked.status_code == 409
    assert "覆盖已有歌词" in blocked.json()["detail"]
