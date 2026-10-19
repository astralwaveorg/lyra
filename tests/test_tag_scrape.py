"""标签刮削：匹配、自动补空、冲突不覆盖、按链接与撤销。

这里用**构造的 MusicBrainz 响应**（字段名照真实响应抄）固定几条边界：

1. 有 MBID 时按发行精确取字段，自动补齐缺失项且音频逐字节不变；
2. 两边都有值且不同 → 冲突，自动流程绝不覆盖；
3. 同目录多数曲目指向一张发行时，缺 MBID 的文件可以补上；
4. 扫描后文件被改动 → 拒绝拿旧提案覆盖；
5. 撤销只还原那次写入涉及的字段；
6. 指纹是最后一道独立证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import lyra.api as api_module
from lyra.api import create_app
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.fingerprint import AcoustIdMatch, Fingerprint
from lyra.flacmeta import flac_audio_hash
from lyra.formats import read_all_tags
from lyra.models import RootRole
from lyra.tagscrape import scan_tags
from lyra.tagsources import (
    ACTION_CONFLICT,
    ACTION_FILL,
    ACTION_SAME,
    MusicBrainzConfig,
    MusicBrainzProvider,
    compare_fields,
)

RELEASE = "11111111-1111-4111-8111-111111111111"
RECORDING = "22222222-2222-4222-8222-222222222222"
TRACK = "33333333-3333-4333-8333-333333333333"
RG = "44444444-4444-4444-8444-444444444444"
WORK = "55555555-5555-4555-8555-555555555555"
ARTIST = "66666666-6666-4666-8666-666666666666"

LENGTH_MS = 3000


def _release_json(*, title: str = "Test Album", label: str = "Test Label") -> dict:
    return {
        "id": RELEASE,
        "title": title,
        "date": "2001-02-03",
        "country": "US",
        "status": "Official",
        "barcode": "0123456789012",
        "asin": "B000TEST",
        "text-representation": {"script": "Latn", "language": "eng"},
        "label-info": [
            {"label": {"name": label, "sort-name": label}, "catalog-number": "CAT-1"}
        ],
        "release-group": {
            "id": RG,
            "title": title,
            "primary-type": "Album",
            "secondary-types": [],
            "first-release-date": "2000-01-01",
        },
        "artist-credit": [
            {"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist", "sort-name": "Artist, Test"}}
        ],
        "media": [
            {
                "position": 1,
                "format": "CD",
                "track-count": 2,
                "tracks": [
                    {
                        "id": TRACK,
                        "number": "1",
                        "title": "Song One",
                        "length": LENGTH_MS,
                        "recording": {
                            "id": RECORDING,
                            "title": "Song One",
                            "artist-credit": [
                                {"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist", "sort-name": "Artist, Test"}}
                            ],
                        },
                    },
                    {
                        "id": "33333333-3333-4333-8333-333333333334",
                        "number": "2",
                        "title": "Song Two",
                        "length": LENGTH_MS,
                        "recording": {"id": "22222222-2222-4222-8222-222222222223", "title": "Song Two"},
                    },
                ],
            }
        ],
    }


def _recording_json() -> dict:
    return {
        "id": RECORDING,
        "title": "Song One",
        "length": LENGTH_MS,
        "isrcs": ["USRC10100001"],
        "artist-credit": [
            {"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist", "sort-name": "Artist, Test"}}
        ],
        "relations": [
            {"type": "performance", "work": {"id": WORK, "title": "Song One"}},
            {"type": "producer", "artist": {"id": ARTIST, "name": "Test Producer"}},
        ],
        "releases": [{"id": RELEASE, "title": "Test Album"}],
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/ws/2/release/{RELEASE}":
        return httpx.Response(200, json=_release_json())
    if path == f"/ws/2/release-group/{RG}":
        return httpx.Response(200, json={
            "id": RG, "title": "Test Album",
            "genres": [{"name": "pop", "count": 8}],
        })
    if path == f"/ws/2/recording/{RECORDING}":
        return httpx.Response(200, json=_recording_json())
    if path == f"/ws/2/work/{WORK}":
        return httpx.Response(200, json={
            "id": WORK, "title": "Song One",
            "relations": [
                {"type": "composer", "artist": {"id": ARTIST, "name": "Composer Person"}},
                {"type": "lyricist", "artist": {"id": ARTIST, "name": "Lyricist Person"}},
            ],
        })
    if path == f"/ws/2/artist/{ARTIST}":
        return httpx.Response(200, json={"id": ARTIST, "name": "Test Artist", "genres": []})
    if path == "/ws/2/release":
        return httpx.Response(200, json={"releases": [
            {"id": RELEASE, "title": "Test Album", "score": 100, "track-count": 2, "date": "2001-02-03"},
        ]})
    if path == "/ws/2/recording":
        return httpx.Response(200, json={"recordings": [
            {
                "id": RECORDING, "title": "Song One", "length": LENGTH_MS, "score": 100,
                "artist-credit": [{"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist"}}],
            },
        ]})
    return httpx.Response(404, json={"error": f"未预置的端点: {path}"})


@pytest.fixture
def provider():
    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(_handler),
        sleep=lambda _s: None,
    )
    yield mb
    mb.close()


def _config(tmp_path: Path, library: Path) -> Config:
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(library))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    cfg.providers.musicbrainz.base_url = "http://mb.test"
    cfg.policy.tags.auto_write = True
    return cfg


def _scan(cfg, provider, *, auto_write=True):
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        return store, scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=auto_write, progress=lambda _m: None,
        )
    finally:
        pass


# ---------------------------------------------------------------------------
# 字段对比
# ---------------------------------------------------------------------------


def test_等价写法不算冲突():
    rows = {
        row["key"]: row for row in compare_fields(
            {
                "TRACKNUMBER": ["01"],
                "DATE": ["2009"],
                "GENRE": ["pop; rock"],
                "ARTISTS": ["A", "B"],
                "TOTALTRACKS": ["10"],
                "TITLE": ["歌"],
            },
            {
                "TRACKNUMBER": ["1"],
                "DATE": ["2009-09-09"],
                "GENRE": ["Pop", "Rock"],
                "ARTISTS": ["B", "A"],
                "TRACKTOTAL": ["10"],
                "TITLE": ["歌"],
            },
        )
    }
    assert rows["TRACKNUMBER"]["action"] == ACTION_SAME
    assert rows["DATE"]["action"] == ACTION_SAME
    assert rows["GENRE"]["action"] == ACTION_SAME
    assert rows["ARTISTS"]["action"] == ACTION_SAME
    # 来源只有别名键时，本地的 TOTALTRACKS 不会单独成行；
    # TRACKTOTAL 这一行必须判为「一致」，而不是「可补」。
    assert rows["TRACKTOTAL"]["action"] == ACTION_SAME
    assert rows["TITLE"]["action"] == ACTION_SAME


def test_排序名分隔符与_MBID_多值写法不算冲突():
    rows = {
        row["key"]: row for row in compare_fields(
            {
                "ARTISTSORT": ["A with B, C"],
                "ALBUMARTISTSORT": ["A & B"],
                "MUSICBRAINZ_ARTISTID": ["id-1 id-2"],
            },
            {
                "ARTISTSORT": ["A; B; C"],
                "ALBUMARTISTSORT": ["A; B"],
                "MUSICBRAINZ_ARTISTID": ["id-1", "id-2"],
            },
        )
    }
    assert rows["ARTISTSORT"]["action"] == ACTION_SAME
    assert rows["ALBUMARTISTSORT"]["action"] == ACTION_SAME
    assert rows["MUSICBRAINZ_ARTISTID"]["action"] == ACTION_SAME

    featuring = {
        row["key"]: row for row in compare_fields(
            {"ARTISTSORT": ["Lee, CoCo featuring Park, Joon Hyung"]},
            {"ARTISTSORT": ["Lee, CoCo; Park, Joon Hyung"]},
        )
    }
    assert featuring["ARTISTSORT"]["action"] == ACTION_SAME


def test_艺人合作写法不同但成员相同不算冲突():
    rows = {
        row["key"]: row for row in compare_fields(
            {"ARTIST": ["李克勤、周慧敏"], "ALBUMARTIST": ["A & B"]},
            {"ARTIST": ["李克勤 / 周慧敏"], "ALBUMARTIST": ["A feat. B"]},
        )
    }
    assert rows["ARTIST"]["action"] == ACTION_SAME
    assert rows["ALBUMARTIST"]["action"] == ACTION_SAME


def test_只缺合作艺人时按补齐处理():
    add = {r["key"]: r for r in compare_fields(
        {"ARTIST": ["主艺人"], "MUSICBRAINZ_ARTISTID": ["id-a"]},
        {"ARTIST": ["主艺人 feat. 合作者"], "MUSICBRAINZ_ARTISTID": ["id-a", "id-b"]},
    )}
    assert add["ARTIST"]["action"] == ACTION_FILL
    assert add["MUSICBRAINZ_ARTISTID"]["action"] == ACTION_FILL

    # 本地比来源多：不能丢掉本地的人
    remove = {r["key"]: r for r in compare_fields(
        {"ARTIST": ["主艺人 feat. 合作者"]},
        {"ARTIST": ["主艺人"]},
    )}
    assert remove["ARTIST"]["action"] == ACTION_CONFLICT


def test_已有流派时完全忽略来源流派():
    """本地已有流派就不该因为「来源多了两个细分风格」被判成冲突。

    默认「如果存在就可以忽略掉」；否则几乎每首歌都会进人工复核。
    """
    rows = {r["key"]: r for r in compare_fields(
        {"GENRE": ["Pop"]},
        {"GENRE": ["Pop", "Blue-eyed Soul", "Pop Soul"]},
    )}
    assert rows["GENRE"]["action"] == ACTION_SAME

    empty = {r["key"]: r for r in compare_fields(
        {"GENRE": []},
        {"GENRE": ["Pop", "Blue-eyed Soul", "Pop Soul"]},
    )}
    assert empty["GENRE"]["action"] == ACTION_FILL
    assert empty["GENRE"]["suggested"] == ["Pop"], "只补一个主流派，不堆一串"


def test_两边不同就是冲突而不是覆盖():
    rows = {r["key"]: r for r in compare_fields(
        {"LABEL": ["Local Label"], "ISRC": []},
        {"LABEL": ["Mb Label"], "ISRC": ["USRC10100001"]},
    )}
    assert rows["LABEL"]["action"] == ACTION_CONFLICT
    assert rows["ISRC"]["action"] == ACTION_FILL


def test_黑胶曲序不写进数字文件():
    from lyra.tagsources import _track_fields

    vinyl = _track_fields(
        {"id": "t", "number": "B5", "title": "X",
         "recording": {"id": "r", "title": "X"}},
        {"position": 1, "format": '12" Vinyl', "track-count": 10},
    )
    assert "TRACKNUMBER" not in vinyl

    digital = _track_fields(
        {"id": "t", "number": "5", "title": "X",
         "recording": {"id": "r", "title": "X"}},
        {"position": 1, "format": "CD", "track-count": 10},
    )
    assert digital["TRACKNUMBER"] == ["5"]


# ---------------------------------------------------------------------------
# 扫描与写入
# ---------------------------------------------------------------------------


def test_按_MBID_自动补空且不覆盖冲突(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One (Live)"),
            ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"),
            ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
            ("LABEL", "Local Label"),   # 发行级冲突：同一录音，可以自动采纳
        ),
    )
    before_audio = flac_audio_hash(target)
    cfg = _config(tmp_path, library)
    store, summary = _scan(cfg, provider)
    try:
        row = store.conn.execute("SELECT * FROM tag_proposals").fetchone()
        assert row is not None
        assert row["state"] == "review", "有冲突时不能自动写入"
        payload = json.loads(row["payload_json"])
        actions = {r["key"]: r["action"] for r in payload["rows"]}
        # 版本不同的标题仍是关键冲突，不能自动改。
        assert actions["TITLE"] == ACTION_CONFLICT
        # 本地 LABEL 与同一录音的发行不一致，属于可采纳的发行级冲突；
        # 补空和纠正在写完后都变成「一致」。
        assert actions["ISRC"] == ACTION_SAME
        assert actions["DATE"] == ACTION_SAME
        assert actions["LABEL"] == ACTION_SAME

        # 待审查里的安全字段会先写掉；关键冲突必须原样保留。
        assert summary.written == 1
        tags, _ = read_all_tags(target)
        assert tags["TITLE"] == ["Song One (Live)"]
        assert tags["LABEL"] == ["Test Label"]
        assert tags["ISRC"] == ["USRC10100001"]
        assert flac_audio_hash(target) == before_audio
    finally:
        store.close()


def test_无冲突时自动写入并保留音频哈希(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"),
            ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"),
            ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
        ),
    )
    before_audio = flac_audio_hash(target)
    cfg = _config(tmp_path, library)
    store, summary = _scan(cfg, provider)
    try:
        assert summary.auto == 1, summary.to_dict()
        assert summary.written == 1, summary.to_dict()
        tags, _ = read_all_tags(target)
        assert tags["ISRC"] == ["USRC10100001"]
        assert tags["LABEL"] == ["Test Label"]
        assert tags["COMPOSER"] == ["Composer Person"]
        assert tags["LYRICIST"] == ["Lyricist Person"]
        assert tags["GENRE"] == ["Pop"]
        assert tags["DATE"] == ["2001-02-03"]
        assert tags["MUSICBRAINZ_RELEASEGROUPID"] == [RG]
        assert tags["TOTALTRACKS"] == ["2"]
        assert flac_audio_hash(target) == before_audio
        assert store.conn.execute("SELECT COUNT(*) c FROM tag_proposals").fetchone()["c"] == 0
        history = store.iter_tag_writes()
        assert len(history) == 1
    finally:
        store.close()


def test_同目录_MBID_可补齐缺_MBID_的曲目(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
        ),
    )
    second = flac_factory(
        library / "Test Artist" / "Test Album" / "02 Song Two.flac",
        entries=(
            ("TITLE", "Song Two"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "2"),
        ),
    )
    cfg = _config(tmp_path, library)
    store, summary = _scan(cfg, provider)
    try:
        tags, _ = read_all_tags(second)
        assert tags.get("MUSICBRAINZ_ALBUMID") == [RELEASE]
        assert tags.get("MUSICBRAINZ_TRACKID") == ["22222222-2222-4222-8222-222222222223"]
        assert summary.matched_album >= 1
    finally:
        store.close()


def test_扫描后文件被改动则拒绝写入(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
            ("LABEL", "Local Label"),
        ),
    )
    cfg = _config(tmp_path, library)
    store, _summary = _scan(cfg, provider, auto_write=False)
    try:
        row = store.conn.execute("SELECT * FROM tag_proposals").fetchone()
        # 模拟扫描后有人改了 LABEL
        from lyra.models import MediaFormat
        from lyra.writer import TagEditPlan, apply_tag_edit

        apply_tag_edit(TagEditPlan(
            audio_path=target,
            media_format=MediaFormat.FLAC,
            tags={"LABEL": ["Someone Else"]},
        ))
        from lyra.tagwrite import apply_proposal

        outcome = apply_proposal(
            store, cfg, cfg.roots[0], store.tag_proposal(int(row["id"])),
            fields=["LABEL"],
        )
        assert not outcome.ok
        assert "已被修改" in (outcome.error or "")
        tags, _ = read_all_tags(target)
        assert tags["LABEL"] == ["Someone Else"]
    finally:
        store.close()


def test_撤销只还原那次写入的字段(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
            ("COMMENT", "keep me"),
        ),
    )
    cfg = _config(tmp_path, library)
    store, summary = _scan(cfg, provider)
    try:
        assert summary.written == 1
        tags, _ = read_all_tags(target)
        assert "ISRC" in tags
        history = store.iter_tag_writes()
        from lyra.tagwrite import undo_tag_write

        outcome = undo_tag_write(store, cfg, cfg.roots[0], store.tag_write(int(history[0]["id"])))
        assert outcome.ok, outcome.error
        tags, _ = read_all_tags(target)
        assert "ISRC" not in tags
        assert tags["COMMENT"] == ["keep me"]
        assert tags["TITLE"] == ["Song One"]
    finally:
        store.close()


def test_只有冲突时补空不会删掉提案(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One (Live)"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
            ("LABEL", "Local Label"),
        ),
    )
    cfg = _config(tmp_path, library)
    store, _summary = _scan(cfg, provider, auto_write=False)
    try:
        row = store.conn.execute("SELECT * FROM tag_proposals").fetchone()
        from lyra.tagwrite import apply_proposal

        # 只写「补空/纠正」：版本标题冲突不在其中，写入后提案必须还在
        outcome = apply_proposal(store, cfg, cfg.roots[0], store.tag_proposal(int(row["id"])))
        assert outcome.ok
        tags, _ = read_all_tags(library / "Test Artist" / "Test Album" / "01 Song One.flac")
        assert tags["TITLE"] == ["Song One (Live)"]
        remaining = store.conn.execute("SELECT COUNT(*) c FROM tag_proposals").fetchone()["c"]
        assert remaining == 1
    finally:
        store.close()


def test_扫描数量上限真的限制读取(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    for index in range(3):
        flac_factory(
            library / "Test Artist" / "Test Album" / f"0{index + 1} Song.flac",
            entries=(("TITLE", f"Song {index + 1}"), ("ARTIST", "Test Artist")),
        )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=False, limit=2, progress=lambda _m: None,
        )
        assert summary.scanned == 2, summary.to_dict()
    finally:
        store.close()


def test_搜索候选时长吻合时自动纠正占位发行信息(tmp_path, flac_factory, provider):
    """没有 MBID、但有真实专辑目录且时长吻合的音频，应当能自动处理。

    本地 `[standalone recordings]` 与 2027 都是占位/无效值，不能拿它们
    阻止纠正；标题、艺人、时长一致时，发行级别字段可以自动写。
    """
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"),
            ("ARTIST", "Test Artist"),
            ("ALBUM", "[standalone recordings]"),
            ("DATE", "2027"),
        ),
    )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=True, progress=lambda _m: None,
        )
        assert summary.written == 1, summary.to_dict()
        tags, _ = read_all_tags(target)
        assert tags["ALBUM"] == ["Test Album"]
        assert tags["DATE"] == ["2001-02-03"]
        assert tags["MUSICBRAINZ_ALBUMID"] == [RELEASE]
        assert tags["MUSICBRAINZ_TRACKID"] == [RECORDING]
        assert tags["TITLE"] == ["Song One"]
    finally:
        store.close()


def test_发行曲目缺时长时用录音时长核验(tmp_path, flac_factory):
    """伪发行/合辑经常没有 track length；录音级时长仍是有效证据。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/ws/2/release/{RELEASE}":
            data = _release_json()
            data["media"][0]["tracks"][0]["length"] = None
            return httpx.Response(200, json=data)
        return _handler(request)

    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(handler), sleep=lambda _s: None,
    )
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"),
        ),
    )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, mb,
            auto_write=True, progress=lambda _m: None,
        )
        assert summary.written == 1, summary.to_dict()
        tags, _ = read_all_tags(target)
        assert tags["MUSICBRAINZ_ALBUMID"] == [RELEASE]
    finally:
        store.close()
        mb.close()


class _FakeFingerprinter:
    available = True

    def compute(self, path):
        return Fingerprint(value="FP", duration=3.0)


class _FakeAcoustId:
    available = True

    def lookup(self, fingerprint, duration):
        return [AcoustIdMatch(score=0.97, recording_ids=(RECORDING,), title="Song One")]

    def close(self):
        pass


def test_没有_MBID_时用指纹确认(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    flac_factory(
        library / "Unknown" / "Unknown Album" / "track01.flac",
        entries=(
            ("TITLE", "Completely Wrong Title"),
            ("ARTIST", "Wrong Artist"),
            ("ALBUM", "Wrong Album"),
        ),
    )
    # 搜索必须查不到，才轮得到指纹
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ws/2/recording" and request.url.params.get("query"):
            return httpx.Response(200, json={"recordings": []})
        return _handler(request)

    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, mb,
            auto_write=False, progress=lambda _m: None,
            fingerprinter=_FakeFingerprinter(), acoustid=_FakeAcoustId(),
        )
        assert summary.fingerprints == 1
        assert summary.matched_fingerprint == 1
        proposal = store.conn.execute("SELECT * FROM tag_proposals").fetchone()
        assert proposal["source"] == "fingerprint"
        payload = json.loads(proposal["payload_json"])
        rows = {row["key"]: row for row in payload["rows"]}
        assert rows["MUSICBRAINZ_TRACKID"]["action"] == ACTION_FILL
        assert rows["MUSICBRAINZ_ALBUMID"]["action"] == ACTION_FILL
        # 本地标题/艺人与来源不同 → 冲突，必须人工确认；指纹只负责「找到对的那条」
        assert rows["TITLE"]["action"] == ACTION_CONFLICT
        assert proposal["state"] == "review"
    finally:
        store.close()
        mb.close()


def test_来源不可用时不覆盖既有提案(tmp_path, flac_factory):
    library = tmp_path / "lib"
    flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(("TITLE", "Song One"), ("ARTIST", "Test Artist")),
    )
    cfg = _config(tmp_path, library)
    cfg.providers.musicbrainz.base_url = ""
    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="", minimum_request_interval_seconds=0)
    )
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, mb,
            auto_write=False, progress=lambda _m: None,
        )
        assert summary.scanned == 0
        assert any("不可用" in item for item in summary.errors)
        assert store.conn.execute("SELECT COUNT(*) c FROM tag_proposals").fetchone()["c"] == 0
    finally:
        store.close()
        mb.close()


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@pytest.fixture
def api_env(tmp_path, flac_factory, monkeypatch):
    library = tmp_path / "lib"
    target = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=(
            ("TITLE", "Song One (Live)"), ("ARTIST", "Test Artist"),
            ("ALBUM", "Test Album"), ("TRACKNUMBER", "1"),
            ("MUSICBRAINZ_ALBUMID", RELEASE),
            ("MUSICBRAINZ_TRACKID", RECORDING),
            ("MUSICBRAINZ_RELEASETRACKID", TRACK),
            ("LABEL", "Local Label"),
        ),
    )
    cfg = _config(tmp_path, library)
    monkeypatch.setattr(
        api_module, "MusicBrainzProvider",
        lambda config, **kw: MusicBrainzProvider(
            config, transport=httpx.MockTransport(_handler), sleep=lambda _s: None
        ),
    )
    store = open_store(cfg.storage.resolved_data_dir)
    scan_tags(cfg, cfg.roots[0], store, MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(_handler), sleep=lambda _s: None,
    ), auto_write=False, progress=lambda _m: None)
    store.close()
    with TestClient(create_app(cfg)) as client:
        client.target = target  # type: ignore[attr-defined]
        client.cfg = cfg  # type: ignore[attr-defined]
        yield client


def test_接口列出提案并可只写勾选字段(api_env):
    summary = api_env.get("/api/v1/tags/summary").json()
    assert summary["counts"]["review"] == 1
    listing = api_env.get("/api/v1/tags/proposals", params={"state": "review"}).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["source"] == "mbid"
    conflicts = [r["key"] for r in item["rows"] if r["action"] == "conflict"]
    assert "TITLE" in conflicts

    response = api_env.post(
        f"/api/v1/tags/proposals/{item['id']}/apply",
        json={"fields": ["DATE", "ISRC", "LABEL"]},
    )
    assert response.status_code == 200, response.text
    tags, _ = read_all_tags(api_env.target)  # type: ignore[attr-defined]
    assert tags["LABEL"] == ["Mb Label"] or tags["LABEL"] == ["Test Label"]
    assert "USRC10100001" in tags["ISRC"]


def test_接口按链接替换候选(api_env):
    listing = api_env.get("/api/v1/tags/proposals").json()
    item = listing["items"][0]
    response = api_env.post(
        f"/api/v1/tags/proposals/{item['id']}/link",
        json={"url": f"https://musicbrainz.org/release/{RELEASE}"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["applied"] is False
    assert data["candidates"]
    response = api_env.post(
        f"/api/v1/tags/proposals/{item['id']}/link",
        json={"url": f"https://musicbrainz.org/release/{RELEASE}", "index": 0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert response.json()["proposal"]["source"] == "link"


def test_接口历史与撤销(api_env):
    listing = api_env.get("/api/v1/tags/proposals").json()
    item = listing["items"][0]
    # 只写一个字段，撤销后应只剩它被还原
    response = api_env.post(
        f"/api/v1/tags/proposals/{item['id']}/apply", json={"fields": ["ISRC"]}
    )
    assert response.status_code == 200, response.text
    history = api_env.get("/api/v1/tags/history").json()["items"]
    assert history and history[0]["applied"].get("ISRC")
    undo = api_env.post(f"/api/v1/tags/writes/{history[0]['id']}/undo")
    assert undo.status_code == 200, undo.text
    tags, _ = read_all_tags(api_env.target)  # type: ignore[attr-defined]
    assert "ISRC" not in tags


def test_批量写入只写可补字段(api_env):
    item = api_env.get("/api/v1/tags/proposals").json()["items"][0]
    response = api_env.post(
        "/api/v1/tags/proposals/bulk-apply",
        json={"ids": [item["id"]], "scope": "fills"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["files"] == 1
    tags, _ = read_all_tags(api_env.target)  # type: ignore[attr-defined]
    # LABEL 现在是同一录音的发行级纠正，属于安全字段；
    # 版本不同的标题仍是冲突，批量补空不能覆盖它。
    assert tags["TITLE"] == ["Song One (Live)"]
    assert tags["LABEL"] == ["Test Label"]
    assert "USRC10100001" in tags["ISRC"]


def test_设置页保存标签策略并立即生效(api_env):
    current = api_env.get("/api/v1/settings/tags").json()["policy"]
    assert current["scan_interval_hours"] == 24.0

    payload = {
        "auto_scan": False,
        "scan_interval_hours": 6,
        "auto_write": False,
        "fingerprint": False,
        "max_auto_write_per_run": 50,
        "max_duration_delta_seconds": 8,
    }
    response = api_env.put("/api/v1/settings/tags", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["policy"]["scan_interval_hours"] == 6.0

    summary = api_env.get("/api/v1/tags/summary").json()
    assert summary["policy"]["auto_scan"] is False
    assert summary["policy"]["scan_interval_hours"] == 6.0
    assert summary["policy"]["max_auto_write_per_run"] == 50

    assert api_env.put(
        "/api/v1/settings/tags", json={**payload, "scan_interval_hours": 0}
    ).status_code == 400
    assert api_env.put(
        "/api/v1/settings/tags", json={**payload, "unknown": 1}
    ).status_code == 400


def test_标签设置写入后重启仍然生效(api_env):
    from lyra.config import TagPolicy

    payload = {
        "auto_scan": True,
        "scan_interval_hours": 9,
        "auto_write": False,
        "fingerprint": True,
        "max_auto_write_per_run": 120,
        "max_duration_delta_seconds": 4,
    }
    assert api_env.put("/api/v1/settings/tags", json=payload).status_code == 200

    cfg = api_env.cfg  # type: ignore[attr-defined]
    cfg.policy.tags = TagPolicy()
    assert cfg.policy.tags.scan_interval_hours == 24.0

    with TestClient(create_app(cfg)) as restarted:
        data = restarted.get("/api/v1/settings/tags").json()
    assert data["policy"]["scan_interval_hours"] == 9.0
    assert data["policy"]["auto_write"] is False
    assert cfg.policy.tags.scan_interval_hours == 9.0


def test_过期提案只写仍为空字段(tmp_path, flac_factory):
    """扫描后文件已有 ARTISTS / 艺人 MBID 时，不应拖累录音 MBID 的写入。

    真实场景：提案是几小时前生成的，期间文件被别的工具补过部分标签。
    旧逻辑「任一字段与扫描时不同就整条失败」，于是剩下的录音 MBID
    永远写不进去，界面还只报「请求参数不正确」。
    """
    from lyra.tagwrite import apply_proposal

    library = tmp_path / "lib"
    target = flac_factory(
        library / "Artist" / "song.flac",
        entries=(
            ("TITLE", "歌"), ("ARTIST", "Artist"),
            ("ARTISTS", "Artist"),
            ("MUSICBRAINZ_ARTISTID", "artist-id"),
        ),
    )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        root_id = store.upsert_root(name="lib", role="library", path=str(library))
        stat = target.stat()
        location_id = store.upsert_location(
            root_id=root_id, rel_path="Artist/song.flac",
            size=stat.st_size, mtime=stat.st_mtime,
        )
        payload = {
            "candidate": {
                "ARTISTS": ["Artist"],
                "MUSICBRAINZ_ARTISTID": ["artist-id"],
                "MUSICBRAINZ_TRACKID": ["recording-id"],
            },
            "rows": [
                {"key": "ARTISTS", "current": [], "suggested": ["Artist"], "action": "fill"},
                {"key": "MUSICBRAINZ_ARTISTID", "current": [], "suggested": ["artist-id"], "action": "fill"},
                {"key": "MUSICBRAINZ_TRACKID", "current": [], "suggested": ["recording-id"], "action": "fill"},
            ],
        }
        proposal_id = store.upsert_tag_proposal(
            location_id=location_id, state="review", source="link",
            confidence=1.0, title="歌", artists="Artist", payload=payload,
        )
        outcome = apply_proposal(
            store, cfg, cfg.roots[0], store.tag_proposal(proposal_id),
            fields=["ARTISTS", "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_TRACKID"],
        )
        assert outcome.ok, outcome.error
        assert set(outcome.applied) == {"MUSICBRAINZ_TRACKID"}
        tags, _ = read_all_tags(target)
        assert tags["ARTISTS"] == ["Artist"]
        assert tags["MUSICBRAINZ_ARTISTID"] == ["artist-id"]
        assert tags["MUSICBRAINZ_TRACKID"] == ["recording-id"]
    finally:
        store.close()
