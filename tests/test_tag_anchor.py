"""目录簇优先：先定专辑锚点，再核验内容。

用户的标准很明确：一个文件夹就是一张专辑。同一首歌即使也出现在精选集里，
只要文件放在正常发行的专辑目录里，发行信息就应以该目录为准；但如果内容
其实是现场版/另一条录音，就不能硬套这个目录的录音室版本。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.flacmeta import flac_audio_hash
from lyra.formats import read_all_tags
from lyra.models import RootRole
from lyra.tagscrape import scan_tags
from lyra.tagsources import ACTION_CORRECT, MusicBrainzConfig, MusicBrainzProvider

ARTIST = "art-1"
RN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"   # 正常专辑
RC = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"   # 精选集
RG_N = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RG_C = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REC1 = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
REC2 = "ffffffff-ffff-4fff-8fff-ffffffffffff"
REC_LIVE = "11111111-2222-4333-8444-555555555555"
T1 = "aaaa1111-1111-4111-8111-111111111111"
T2 = "aaaa2222-2222-4222-8222-222222222222"
T_LIVE = "aaaa3333-3333-4333-8333-333333333333"


def _release(release_id: str, title: str, rg_id: str, date: str, label: str,
             secondary: list[str], tracks: list[tuple[str, str, str, int]]) -> dict:
    return {
        "id": release_id,
        "title": title,
        "date": date,
        "country": "US",
        "status": "Official",
        "barcode": "123",
        "text-representation": {"script": "Latn"},
        "label-info": [{"label": {"name": label}, "catalog-number": "CAT"}],
        "release-group": {
            "id": rg_id, "title": title, "primary-type": "Album",
            "secondary-types": secondary, "first-release-date": date,
        },
        "artist-credit": [
            {"name": "Test Artist", "artist": {"id": ARTIST, "name": "Test Artist",
                                               "sort-name": "Artist, Test"}}
        ],
        "media": [{
            "position": 1, "format": "CD", "track-count": len(tracks),
            "tracks": [
                {
                    "id": track_id, "number": str(index + 1), "title": title_,
                    "length": 3000,
                    "recording": {
                        "id": rec_id, "title": title_, "isrcs": [f"US{index:010d}"],
                        "artist-credit": [
                            {"name": "Test Artist",
                             "artist": {"id": ARTIST, "name": "Test Artist",
                                        "sort-name": "Artist, Test"}}
                        ],
                    },
                }
                for index, (track_id, rec_id, title_, _length) in enumerate(tracks)
            ],
        }],
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/ws/2/release/{RN}":
        return httpx.Response(200, json=_release(
            RN, "Test Album", RG_N, "2001-02-03", "Normal Label", [],
            [(T1, REC1, "Song One", 3000), (T2, REC2, "Song Two", 3000)],
        ))
    if path == f"/ws/2/release/{RC}":
        return httpx.Response(200, json=_release(
            RC, "Test Compilation", RG_C, "2010-01-01", "Comp Label", ["Compilation"],
            [(T_LIVE, REC_LIVE, "Song Two (Live)", 3000), (T1, REC1, "Song One", 3000)],
        ))
    if path.startswith("/ws/2/recording/"):
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "isrcs": [],
                                         "relations": []})
    if path.startswith("/ws/2/release-group/"):
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "genres": [],
                                         "releases": []})
    if path == "/ws/2/release":
        return httpx.Response(200, json={"releases": [
            {"id": RN, "title": "Test Album", "score": 100, "track-count": 2,
             "date": "2001-02-03"},
        ]})
    if path == "/ws/2/recording":
        return httpx.Response(200, json={"recordings": []})
    return httpx.Response(404, json={"error": path})


@pytest.fixture
def provider():
    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0),
        transport=httpx.MockTransport(_handler), sleep=lambda _s: None,
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
    cfg.policy.tags.auto_write = False
    return cfg


def _entries(recording: str, album_mbid: str, rg: str, *, title: str,
             track: str, date: str, label: str, track_title: str | None = None) -> tuple:
    return (
        ("TITLE", track_title or title), ("ARTIST", "Test Artist"),
        ("ALBUM", "Test Album"), ("TRACKNUMBER", track),
        ("MUSICBRAINZ_ALBUMID", album_mbid),
        ("MUSICBRAINZ_RELEASEGROUPID", rg),
        ("MUSICBRAINZ_TRACKID", recording),
        ("DATE", date), ("LABEL", label), ("RELEASETYPE", "album"),
    )


def test_同目录优先纠正精选集来源(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    target1 = flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=_entries(REC1, RC, RG_C, title="Song One", track="1",
                         date="2010-01-01", label="Comp Label"),
    )
    target2 = flac_factory(
        library / "Test Artist" / "Test Album" / "02 Song Two.flac",
        entries=_entries(REC2, RC, RG_C, title="Song Two", track="2",
                         date="2010-01-01", label="Comp Label"),
    )
    cfg = _config(tmp_path, library)
    cfg.policy.tags.auto_write = True
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=True, progress=lambda _m: None,
        )
        assert summary.written == 2, summary.to_dict()
        for target in (target1, target2):
            tags, _ = read_all_tags(target)
            assert tags["MUSICBRAINZ_ALBUMID"] == [RN], "应按目录改回正常专辑"
            assert tags["ALBUM"] == ["Test Album"]
            assert tags["DATE"] == ["2001-02-03"]
            assert tags["LABEL"] == ["Normal Label"]
            assert tags["MUSICBRAINZ_RELEASEGROUPID"] == [RG_N]
            assert tags["RELEASETYPE"] == ["album"]
            assert flac_audio_hash(target)
    finally:
        store.close()


def test_内容是另一条录音时不按目录纠正(tmp_path, flac_factory, provider):
    library = tmp_path / "lib"
    live = flac_factory(
        library / "Test Artist" / "Test Album" / "02 Song Two.flac",
        entries=_entries(
            REC_LIVE, RC, RG_C, title="Song Two", track="2",
            date="2010-01-01", label="Comp Label", track_title="Song Two (Live)",
        ),
    )
    flac_factory(
        library / "Test Artist" / "Test Album" / "01 Song One.flac",
        entries=_entries(REC1, RN, RG_N, title="Song One", track="1",
                         date="2001-02-03", label="Normal Label"),
    )
    cfg = _config(tmp_path, library)
    store = open_store(cfg.storage.resolved_data_dir)
    try:
        summary = scan_tags(
            cfg, cfg.roots[0], store, provider,
            auto_write=False, progress=lambda _m: None,
        )
        assert summary.scanned == 2
        tags, _ = read_all_tags(live)
        assert tags["MUSICBRAINZ_ALBUMID"] == [RC], "内容不同不能硬套目录专辑"
        row = store.conn.execute(
            "SELECT payload_json FROM tag_proposals p JOIN locations l "
            "ON l.id=p.location_id WHERE l.rel_path LIKE '%Song Two%'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        assert not [r for r in payload["rows"] if r["action"] == ACTION_CORRECT]
        assert payload["match"]["release_id"] == RC
    finally:
        store.close()
