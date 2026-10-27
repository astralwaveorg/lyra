"""按「推荐最高分」「歌词必须有时间轴」「按艺人筛选」三件事的回归。

这三件事是同一轮改动的三个面：

1. **推荐 = 分数最高**。原先复核页把「安全」的候选排在前面，于是最优的
   0.86 被跳过、推荐了 0.83——使用者看到的是「系统推荐错了」。
2. **歌词必须有时间轴**。最优候选的歌词可能只有纯文本（LRCLIB 上同一首歌
   的不同版本，有的有同步歌词、有的没有）。旧实现遇到这种情况**整首放弃**，
   下面那条有时间轴的候选根本没被试过。
3. **按艺人筛选**（包含即命中）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import lyra.providers as providers_module
from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import _artist_matches

# ---------------------------------------------------------------------------
# 按艺人筛选
# ---------------------------------------------------------------------------


def test_艺人包含即命中() -> None:
    # 选一个包含在多艺人署名里的名字 → 命中
    assert _artist_matches("The Beatles", ("Tony Sheridan / The Beatles",))
    assert _artist_matches("the beatles", ("The Beatles",))
    # 反过来：选更长的串也算命中
    assert _artist_matches("The Beatles / Tony Sheridan", ("The Beatles",))
    # 完全不是同一个人 → 不命中
    assert not _artist_matches("Adele", ("The Beatles",))
    # 空条件 → 全都命中
    assert _artist_matches("", ("Adele",))


def test_按艺人筛选只处理该艺人的曲目(
    tmp_path: Path, flac_factory, monkeypatch
) -> None:
    lib = tmp_path / "lib"
    flac_factory(lib / "A" / "a.flac", entries=(("TITLE", "a"), ("ARTIST", "Adele")))
    flac_factory(
        lib / "B" / "b.flac",
        entries=(("TITLE", "b"), ("ARTIST", "The Beatles / Tony Sheridan")),
    )
    monkeypatch.setattr(providers_module, "create_providers", lambda *a, **k: [])
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/v1/library/artists").status_code == 200
        artists = client.get("/api/v1/library/artists").json()["items"]
        names = {item["name"] for item in artists}
        assert "Adele" in names
        assert "The Beatles" in names or "Tony Sheridan" in names

        started = client.post(
            "/api/v1/tasks/fetch",
            json={"filters": {"artist": "The Beatles"}, "apply": False},
        ).json()
        # 任务跑完：只处理了匹配的曲目
        for _ in range(50):
            done = client.get(f"/api/v1/tasks/{started['id']}").json()
            if done["state"] in ("done", "failed"):
                break
        assert done["state"] == "done", done
        entry = done["result"]["roots"][0]
        assert entry["considered"] == 1, entry


# ---------------------------------------------------------------------------
# 推荐 = 分数最高
# ---------------------------------------------------------------------------


def test_复核列表按分数排序而不是把安全的排前面(
    tmp_path: Path, flac_factory
) -> None:
    """同一首歌有两个候选时，界面上第一个就是分数最高的那个。"""
    lib = tmp_path / "lib"
    flac_factory(lib / "歌.flac", entries=(("TITLE", "歌"), ("ARTIST", "A")))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    with TestClient(create_app(cfg)) as client:
        store = open_store(cfg.storage.resolved_data_dir)
        try:
            root_id = store.upsert_root(
                name="lib", role="library", path=str(lib), enabled=True, read_only=False
            )
            loc = store.upsert_location(
                root_id=root_id, rel_path="歌.flac", size=1, mtime=1.0
            )
            # 分数低的先写、高的后写：排序必须按分数还原，而不是按写入顺序
            store.stage_lyrics(
                location_id=loc, content_hash="h1", content="[00:01.00]低\n",
                provider="qq", provider_id="low", score=0.50, state="needs_review",
            )
            store.stage_lyrics(
                location_id=loc, content_hash="h2", content="[00:01.00]高\n",
                provider="netease", provider_id="high", score=0.86, state="needs_review",
            )
        finally:
            store.close()

        page = client.get("/api/v1/staged", params={"state": "needs_review"}).json()
        group = page["groups"][0]
        scores = [c["score"] for c in group["candidates"]]
        assert scores == sorted(scores, reverse=True), scores
        assert group["candidates"][0]["provider"] == "netease"
