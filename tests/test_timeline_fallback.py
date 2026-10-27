"""自动刮削：最优候选没有时间轴时要退到下一条，而不是整首放弃。

实际故障（`07 Kansas City.flac`）：最优的网易候选 0.86 只有纯文本歌词，
于是整首歌什么都没抓到——而分数更高的 LRCLIB 候选是有时间轴的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.config import Config, RootConfig, StorageConfig
from lyra.db import open_store
from lyra.formats import MediaFormat
from lyra.models import RootRole
from lyra.pipeline import TrackTarget, _lyrics_with_timeline
from lyra.providers.base import Candidate, RawLyrics


class _StubProvider:
    """按 provider_id 返回歌词：plain 的那条没有时间轴。"""

    name = "stub"

    def __init__(self, lyrics: dict[str, RawLyrics]) -> None:
        self._lyrics = lyrics

    def lyrics(self, candidate: Candidate) -> RawLyrics | None:
        return self._lyrics.get(candidate.provider_id)


class _Score:
    """最小可用的候选打分对象。"""

    def __init__(self, provider: str, provider_id: str, score: float) -> None:
        self.provider = provider
        self.provider_id = provider_id
        self.score = score
        self.duration_delta = 0.0
        self.rejected = False


@pytest.fixture
def ctx(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    from lyra.match import build_context

    context = build_context({"TITLE": ["Kansas City"], "ARTIST": ["The Beatles"]}, 160.0)
    target = TrackTarget(
        location_id=0,
        rel_path="Kansas City.flac",
        abs_path=lib / "Kansas City.flac",
        context=context,
        revision_hash="",
        media_format=MediaFormat.FLAC,
    )
    yield store, cfg, target
    store.close()


def test_最优没有时间轴时退到下一条(ctx) -> None:
    store, cfg, target = ctx
    candidates = [
        Candidate(provider="stub", provider_id="plain", title="Kansas City",
                  artists=("The Beatles",), album="", duration=160.0),
        Candidate(provider="stub", provider_id="synced", title="Kansas City",
                  artists=("The Beatles",), album="", duration=160.0),
    ]
    scores = [_Score("stub", "plain", 0.86), _Score("stub", "synced", 0.98)]
    provider = _StubProvider({
        "plain": RawLyrics(provider="stub", provider_id="plain",
                           text="作曲：Tony Sheridan\n", synced=False),
        "synced": RawLyrics(provider="stub", provider_id="synced",
                            text="[00:01.00]Kansas City\n", synced=True),
    })

    lyrics, used, error = _lyrics_with_timeline(
        {"stub": provider}, candidates, scores, store, cfg, target
    )
    assert lyrics is not None
    assert used is not None and used.provider_id == "synced"
    assert error is None
    assert lyrics.synced is True


def test_全部没有时间轴时如实报告(ctx) -> None:
    store, cfg, target = ctx
    candidates = [
        Candidate(provider="stub", provider_id="plain", title="Kansas City",
                  artists=("The Beatles",), album="", duration=160.0),
    ]
    scores = [_Score("stub", "plain", 0.86)]
    provider = _StubProvider({
        "plain": RawLyrics(provider="stub", provider_id="plain",
                           text="作曲：Tony Sheridan\n", synced=False),
    })
    lyrics, used, error = _lyrics_with_timeline(
        {"stub": provider}, candidates, scores, store, cfg, target
    )
    assert lyrics is None
    assert used is None
