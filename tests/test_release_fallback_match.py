"""发行级兜底搜索：录音搜索没把握时，按「艺人 + 专辑」再查一次。

## 为什么需要这条路径

录音搜索对华语条目覆盖差：本地「爱如少年」，MusicBrainz 上可能写作
「愛如少年」或挂在合辑下，标题+艺人搜索常常搜不到或者证据不足。
但**专辑页**通常存在且曲目列表完整——按「艺人+专辑」搜发行、在发行内
按标题定位，是同一首歌的第二条独立证据路径。

定位判据：标题规范化后相似 ≥0.92（简繁/大小写/标点差异不算不同）。
"""

from __future__ import annotations

from pathlib import Path

from lyra.tagscrape import TrackSnapshot, _release_level_fallback
from lyra.tagsources import MusicBrainzProvider


def _snap(tmp_path: Path) -> TrackSnapshot:
    return TrackSnapshot(
        location_id=1,
        rel_path="a.flac",
        path=tmp_path / "a.flac",
        tags={"TITLE": ["爱如少年"], "ARTIST": ["许巍"], "ALBUM": ["爱如少年"]},
        duration=240.0,
        media_format="flac",
        writable=True,
        dir=".",
        file_size=1,
        file_mtime=1,
        title="爱如少年",
        artists=("许巍",),
        album="爱如少年",
        albumartist="许巍",
    )


class _FakeProvider:
    """发行搜索命中、发行内能按标题定位到曲目。"""

    name = "mb"
    available = True

    def search_releases(self, *, album: str, artist: str = "", limit: int = 8):
        assert album == "爱如少年"
        return [{"id": "rel-1"}]

    def release_detail(self, release_id: str):
        return {
            "id": "rel-1",
            "title": "爱如少年",
            "media": [
                {
                    "position": 1,
                    "track-count": 2,
                    "tracks": [
                        {
                            "id": "tr-1",
                            "number": "1",
                            "title": "愛如少年",  # 繁体——归一后应命中
                            "length": 240000,
                            "recording": {"id": "rec-1"},
                        },
                    ],
                }
            ],
        }

    def genres_for_release(self, release):
        return ()


class _EmptyProvider:
    name = "mb"
    available = True

    def search_releases(self, *, album: str, artist: str = "", limit: int = 8):
        return []

    def release_detail(self, release_id: str):
        return None


def test_发行内按标题定位命中(tmp_path: Path):
    """标题简繁不同、归一后相同 → 命中并返回候选。"""

    provider = _FakeProvider()
    assert isinstance(provider, MusicBrainzProvider) or True  # 类型仅为文档
    snap = _snap(tmp_path)

    class _Hints:
        album = "爱如少年"

    candidate, releases, ambiguous = _release_level_fallback(
        provider, snap, _Hints(), False,  # type: ignore[arg-type]
    )
    assert candidate is not None, "应通过发行定位到曲目"
    # 标题简繁不同但归一后相同——命中（返回侧可能已做归一）
    assert "如少年" in candidate.title
    assert releases


def test_搜索无结果时返回空(tmp_path: Path):
    snap = _snap(tmp_path)

    class _Hints:
        album = "爱如少年"

    candidate, releases, ambiguous = _release_level_fallback(
        _EmptyProvider(), snap, _Hints(), False,  # type: ignore[arg-type]
    )
    assert candidate is None
    assert releases == []
    assert ambiguous is False


def test_无专辑线索时跳过兜底(tmp_path: Path):
    snap = _snap(tmp_path)

    class _Hints:
        album = ""

    candidate, _releases, _ambiguous = _release_level_fallback(
        _FakeProvider(), snap, _Hints(), False,  # type: ignore[arg-type]
    )
    assert candidate is None, "没有专辑线索就不走发行级兜底"
