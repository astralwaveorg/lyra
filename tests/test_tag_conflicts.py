"""冲突自动采纳：只有确认同一录音、且差异不是关键内容时才自动。

这组测试固定几条边界：作曲家被误写成艺术家、标点差异、艺人改名、
本地多出合作艺人。最后一条必须继续人工确认，不能为了自动而丢人。
"""

from __future__ import annotations

from pathlib import Path

from lyra.tagscrape import (
    SOURCE_MBID,
    MatchResult,
    TrackSnapshot,
    _auto_resolve_conflicts,
)
from lyra.tagsources import ACTION_CONFLICT, ACTION_CORRECT, compare_fields


def _snap(tags: dict[str, list[str]]) -> TrackSnapshot:
    return TrackSnapshot(
        location_id=1,
        rel_path="a.flac",
        path=Path("a.flac"),
        tags=tags,
        duration=300.0,
        media_format="flac",
        writable=True,
        dir=".",
        file_size=1,
        file_mtime=1,
        title=(tags.get("TITLE") or [""])[0],
        artists=tuple(tags.get("ARTISTS") or tags.get("ARTIST") or ()),
        album=(tags.get("ALBUM") or [""])[0],
        albumartist=(tags.get("ALBUMARTIST") or [""])[0],
    )


def _resolve(local: dict[str, list[str]], remote: dict[str, list[str]]) -> dict[str, dict]:
    snap = _snap(local)
    rows = compare_fields(local, remote)
    out = _auto_resolve_conflicts(
        rows,
        MatchResult(source=SOURCE_MBID, confidence=1.0),
        snap,
    )
    return {row["key"]: row for row in out}


def test_作曲家误写成艺术家时采纳远程演奏者():
    local = {
        "ARTIST": ["Arvo Pärt"], "ARTISTS": ["Arvo Pärt"],
        "ARTISTSORT": ["Pärt, Arvo"],
        "MUSICBRAINZ_ARTISTID": ["composer-id"],
        "COMPOSER": ["Arvo Pärt"],
        "ALBUMARTIST": ["Arvo Pärt; Göteborgs Symfoniker"],
    }
    remote = {
        "ARTIST": ["Göteborgs Symfoniker, Neeme Järvi"],
        "ARTISTS": ["Göteborgs Symfoniker", "Neeme Järvi"],
        "ARTISTSORT": ["Göteborgs Symfoniker; Järvi, Neeme"],
        "MUSICBRAINZ_ARTISTID": ["orch-id", "cond-id"],
    }
    rows = _resolve(local, remote)
    for key in ("ARTIST", "ARTISTS", "ARTISTSORT", "MUSICBRAINZ_ARTISTID"):
        assert rows[key]["action"] == ACTION_CORRECT, key


def test_标题只差标点时采纳远程():
    local = {"TITLE": ["G.E.M. Rolling In The Deep"]}
    remote = {"TITLE": ["G.E.M. / Rolling In The Deep"]}
    rows = _resolve(local, remote)
    assert rows["TITLE"]["action"] == ACTION_CORRECT


def test_艺人改名但_MBID_相同则采纳远程():
    local = {
        "ARTIST": ["Taylor Swift feat. Dixie Chicks"],
        "ARTISTS": ["Taylor Swift", "Dixie Chicks"],
        "MUSICBRAINZ_ARTISTID": ["ts-id", "chicks-id"],
    }
    remote = {
        "ARTIST": ["Taylor Swift featuring The Chicks"],
        "ARTISTS": ["Taylor Swift", "The Chicks"],
        "MUSICBRAINZ_ARTISTID": ["ts-id", "chicks-id"],
    }
    rows = _resolve(local, remote)
    assert rows["ARTIST"]["action"] == ACTION_CORRECT
    assert rows["ARTISTS"]["action"] == ACTION_CORRECT


def test_本地多出合作艺人时不自动丢人():
    local = {
        "ARTIST": ["李玟 feat. 吳建豪"],
        "ARTISTS": ["李玟", "吳建豪"],
        "MUSICBRAINZ_ARTISTID": ["coco-id", "vanness-id"],
        "ALBUMARTIST": ["李玟"],
    }
    remote = {
        "ARTIST": ["李玟"],
        "ARTISTS": ["李玟"],
        "MUSICBRAINZ_ARTISTID": ["coco-id"],
    }
    rows = _resolve(local, remote)
    assert rows["ARTIST"]["action"] == ACTION_CONFLICT
    assert rows["ARTISTS"]["action"] == ACTION_CONFLICT


def test_本地是专辑艺术家简写时采纳原始署名():
    local = {
        "ARTIST": ["The Beatles"], "ARTISTS": ["The Beatles"],
        "MUSICBRAINZ_ARTISTID": ["beatles-id"],
        "ALBUMARTIST": ["The Beatles"],
        "COMPOSER": ["Traditional"],
    }
    remote = {
        "ARTIST": ["Tony Sheridan and The Beat Brothers"],
        "ARTISTS": ["Tony Sheridan", "The Beat Brothers"],
        "MUSICBRAINZ_ARTISTID": ["tony-id", "brothers-id"],
    }
    rows = _resolve(local, remote)
    assert rows["ARTIST"]["action"] == ACTION_CORRECT
    assert rows["ARTISTS"]["action"] == ACTION_CORRECT


def test_艺人名只差前缀时采纳远程写法():
    local = {
        "ARTIST": ["MC Jin, Gold Mountain"],
        "ARTISTS": ["MC Jin", "Gold Mountain"],
        "MUSICBRAINZ_ARTISTID": ["jin-id", "gold-id"],
    }
    remote = {
        "ARTIST": ["MC Jin, MC Gold Mountain"],
        "ARTISTS": ["MC Jin", "MC Gold Mountain"],
        "MUSICBRAINZ_ARTISTID": ["jin-id", "mc-gold-id"],
    }
    rows = _resolve(local, remote)
    assert rows["ARTIST"]["action"] == ACTION_CORRECT
    assert rows["ARTISTS"]["action"] == ACTION_CORRECT
