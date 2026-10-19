"""标签刮削：全库扫描 → 逐字段提案 → 自动补空 / 人工复核。

## 匹配优先级（从强到弱）

1. **同目录专辑簇**：一个目录先被当成一张专辑，选一个能解释整目录的
   「锚点发行」。逐首优先用锚点发行的专辑级字段（专辑、MBID、日期、
   厂牌、曲序），即使文件自己的 MBID 指向另一张精选集。
2. **内容核验**：只有内容确实是同一录音（录音 ID 相同，或标题+时长+
   版本标记一致）才允许按目录纠正；现场版/混音版放进录音室专辑这类
   内容差异会被识别出来，退回该文件自己的证据。
3. **文件自己的 MBID**：没有可用锚点，或锚点里找不到这首歌时，按文件
   自己的 MBID 精确查询。
4. **标题 + 艺人搜索**：只在没有 MBID 时兜底；有歧义就送人工，不猜。
5. **AcoustID 指纹**：以上都失败时，用音频本身确认录音 ID；它用于
   **核验内容**，不用于推翻一个可信的目录锚点。

## 写入边界

自动写入只补缺失，或按可信目录簇纠正**发行级字段**（专辑、MBID、日期、
厂牌、曲序、发行类型等）；标题、艺人、ISRC、人员、流派这些内容级字段
不会因为目录而覆盖。其余冲突一律进人工复核。
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .audit import audit_root
from .config import Config, RootConfig
from .db import StateStore
from .fingerprint import (
    AcoustIdClient,
    FingerprintError,
    FpcalcFingerprinter,
)
from .formats import read_all_tags
from .match import build_context, extract_version_tokens, normalize_text
from .models import MediaFormat
from .tagsources import (
    ACTION_CONFLICT,
    ACTION_CORRECT,
    ACTION_FILL,
    EntityLink,
    MusicBrainzProvider,
    _artist_tokens,
    build_release_candidate,
    compare_fields,
    locate_release_track,
    lookup_by_link,
    summarise,
)

__all__ = [
    "SOURCE_LABELS",
    "STATE_AUTO",
    "STATE_REVIEW",
    "STATE_UNMATCHED",
    "TagScanSummary",
    "TrackSnapshot",
    "scan_tags",
    "snapshot_for",
    "proposal_payload_from_match",
]

#: 目录锚点可以纠正的**发行级**字段。内容级字段（标题/艺人/ISRC/人员）
#: 不在其中：它们描述这首歌本身，不随专辑归属变化。
CORRECTABLE_FIELDS = frozenset({
    "ALBUM", "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_RELEASETRACKID",
    "DATE", "ORIGINALDATE", "ORIGINALYEAR",
    "LABEL", "CATALOGNUMBER", "BARCODE", "ASIN",
    "RELEASECOUNTRY", "RELEASESTATUS", "RELEASETYPE", "MEDIA", "SCRIPT",
    "TOTALDISCS", "DISCTOTAL", "TOTALTRACKS", "TRACKTOTAL",
    "TRACKNUMBER", "DISCNUMBER",
    "ALBUMARTIST", "ALBUMARTISTS", "ALBUMARTISTSORT",
    "MUSICBRAINZ_ALBUMARTISTID",
})

STATE_AUTO = "auto"
STATE_REVIEW = "review"
STATE_UNMATCHED = "unmatched"
STATE_SKIPPED = "skipped"

SOURCE_MBID = "mbid"
SOURCE_ALBUM = "album"
SOURCE_SEARCH = "search"
SOURCE_FINGERPRINT = "fingerprint"
SOURCE_LINK = "link"

SOURCE_LABELS = {
    SOURCE_MBID: "MBID 精确",
    SOURCE_ALBUM: "同目录推定",
    SOURCE_SEARCH: "标题艺人搜索",
    SOURCE_FINGERPRINT: "音频指纹",
    SOURCE_LINK: "链接指定",
}

#: 自动写入的**最低**置信度。低于此值即便没有冲突也进复核队列——
#: 标签写错的影响面比歌词大（整张专辑的年份、厂牌会一起错）。
AUTO_MIN_CONFIDENCE = 0.9

_GENERIC_FOLDER = re.compile(
    r"^(?:cd|disc|disk|vol(?:ume)?)[\s._-]*\d+$", re.IGNORECASE
)
_TRACK_PREFIX = re.compile(r"^\s*(?:\d{1,3}[\s._-]+)+")


def _first(tags: Mapping[str, Sequence[str]], key: str) -> str:
    values = tags.get(key) or []
    return str(values[0]).strip() if values else ""


def _sim(a: str, b: str) -> float:
    """规范化后的相似度。简繁、大小写、标点差异在这里被抹平。"""
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _clean_stem(path: Path) -> str:
    return _TRACK_PREFIX.sub("", path.stem).strip() or path.stem


def _duration_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return abs(float(left) - float(right))


@dataclass(slots=True)
class TrackSnapshot:
    location_id: int
    rel_path: str
    path: Path
    tags: dict[str, list[str]]
    duration: float | None
    media_format: str
    writable: bool
    dir: str
    file_size: int
    file_mtime: int

    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    albumartist: str = ""
    album_id: str = ""
    recording_id: str = ""
    release_track_id: str = ""
    track_number: str = ""
    disc_number: str = ""

    def track_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "albumartist": self.albumartist,
            "duration": self.duration,
            "format": self.media_format,
            "writable": self.writable,
            "dir": self.dir,
            "name": self.path.name,
        }


@dataclass(slots=True)
class FolderAnchor:
    """一个目录的专辑锚点：能解释整目录的那张发行。"""

    release_id: str
    release: dict
    coverage: float
    score: float
    method: str = "mbid"
    trusted: bool = False
    kinds: frozenset[str] = frozenset()
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    source: str
    confidence: float
    release_id: str = ""
    anchor_trusted: bool = False
    content_strength: float = 0.0
    verified: bool = False
    verification: str = ""
    recording_id: str = ""
    album: str = ""
    duration_delta: float | None = None
    fields: dict[str, list[str]] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    candidates: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class TagScanSummary:
    scanned: int = 0
    skipped: int = 0
    matched_mbid: int = 0
    matched_album: int = 0
    matched_search: int = 0
    matched_fingerprint: int = 0
    unresolved: int = 0
    satisfied: int = 0
    auto: int = 0
    review: int = 0
    written: int = 0
    written_fields: int = 0
    fingerprints: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "skipped": self.skipped,
            "matched_mbid": self.matched_mbid,
            "matched_album": self.matched_album,
            "matched_search": self.matched_search,
            "matched_fingerprint": self.matched_fingerprint,
            "unresolved": self.unresolved,
            "satisfied": self.satisfied,
            "auto": self.auto,
            "review": self.review,
            "written": self.written,
            "written_fields": self.written_fields,
            "fingerprints": self.fingerprints,
            "errors": self.errors[:30],
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(slots=True)
class _GroupHints:
    album: str = ""
    artist: str = ""
    dominant_album_id: str = ""
    album_ids: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def snapshot_for(
    root: RootConfig,
    location_id: int,
    rel_path: str,
    path: Path,
    tags: dict[str, list[str]],
    duration: float | None,
    media_format: str,
    size: int,
    mtime: int,
) -> TrackSnapshot:
    context = build_context(tags, duration)
    title = context.title or _clean_stem(path)
    album = context.album
    snap = TrackSnapshot(
        location_id=location_id,
        rel_path=rel_path,
        path=path,
        tags=tags,
        duration=duration,
        media_format=media_format,
        writable=(
            media_format in (
                MediaFormat.FLAC.value,
                MediaFormat.MP3.value,
                MediaFormat.OGG_VORBIS.value,
                MediaFormat.OGG_OPUS.value,
            )
            and not root.read_only
        ),
        dir=str(Path(rel_path).parent),
        file_size=size,
        file_mtime=mtime,
        title=title,
        artists=context.artists,
        album=album,
        albumartist=_first(tags, "ALBUMARTIST") or _first(tags, "ALBUM ARTIST"),
        album_id=_first(tags, "MUSICBRAINZ_ALBUMID"),
        recording_id=_first(tags, "MUSICBRAINZ_TRACKID"),
        release_track_id=_first(tags, "MUSICBRAINZ_RELEASETRACKID"),
        track_number=_first(tags, "TRACKNUMBER"),
        disc_number=_first(tags, "DISCNUMBER"),
    )
    return snap


def _hints(group: Sequence[TrackSnapshot], snap: TrackSnapshot, parent_name: str) -> _GroupHints:
    albums = Counter(s.album for s in group if s.album.strip())
    artists = Counter(s.albumartist for s in group if s.albumartist.strip())
    album = albums.most_common(1)[0][0] if albums else ""
    folder = Path(snap.dir).name
    if not album:
        album = "" if _GENERIC_FOLDER.match(folder) else folder
    artist = artists.most_common(1)[0][0] if artists else (snap.artists[0] if snap.artists else parent_name)
    ids = Counter(s.album_id for s in group if s.album_id.strip())
    dominant = ""
    if ids:
        candidate, count = ids.most_common(1)[0]
        # 单曲目录也允许用自己那一条；多曲目录必须过半数，避免把
        # 「混进来的单曲」当成整张专辑。
        # 至少一半的曲目指向同一张发行才算「目录共识」。
        # 两首的目录里 1 首有 MBID 也算——这是补全半个标签的常见情况。
        if len(group) == 1 or count * 2 >= len(group):
            dominant = candidate
    return _GroupHints(album=album, artist=artist, dominant_album_id=dominant, album_ids=ids)


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


_CREDIT_KEYS = (
    "COMPOSER", "LYRICIST", "ARRANGER", "PRODUCER", "ENGINEER", "MIXER",
    "CONDUCTOR", "REMIXER",
)


def _locate_in_release(
    provider: MusicBrainzProvider,
    snap: TrackSnapshot,
    release_id: str,
    store: StateStore | None = None,
) -> tuple[dict, dict, str, dict[str, list[str]], dict[str, str]] | None:
    release = provider.release_detail(release_id)
    if release is None:
        return None
    track, medium, method = locate_release_track(
        release,
        release_track_id=snap.release_track_id,
        recording_id=snap.recording_id,
        title=snap.title,
        duration=snap.duration,
        track_number=snap.track_number,
        disc_number=snap.disc_number,
    )
    if track is None:
        return None
    # 本地已经有的补充字段不必再向来源查询：作曲/流派要额外请求 1~2 次，
    # 全库都查会让一次扫描多出几千次调用，而结果根本不会被写入。
    has_credits = any(snap.tags.get(key) for key in _CREDIT_KEYS)
    recording = track.get("recording")
    recording_id = str(
        (recording if isinstance(recording, dict) else {}).get("id") or ""
    )
    cached_credits = (
        store.cached_recording_credits(recording_id)
        if store is not None and recording_id and not has_credits
        else None
    )
    fields, extra = build_release_candidate(
        provider,
        release,
        release_id,
        track,
        medium,
        include_credits=not has_credits and cached_credits is None,
        include_genres=not bool(snap.tags.get("GENRE")),
    )
    if cached_credits:
        fields.update(cached_credits)
    elif store is not None and recording_id and not has_credits:
        learned = {key: fields[key] for key in _CREDIT_KEYS if fields.get(key)}
        if learned:
            store.save_recording_credits(recording_id, learned)
    return release, track, method, fields, extra


def _locate_confidence(method: str, delta: float | None) -> float:
    if method in ("release_track_id", "recording_id"):
        return 1.0
    if method in ("title+duration", "title", "duration", "number+duration", "number", "mixed"):
        if delta is None:
            return 0.9
        if delta <= 2.0:
            return 0.97
        if delta <= 5.0:
            return 0.93
        if delta <= 10.0:
            return 0.86
        return 0.8
    return 0.7


def _match_from_release(
    provider: MusicBrainzProvider,
    snap: TrackSnapshot,
    release_id: str,
    *,
    source: str,
    base_confidence: float,
    notes: Sequence[str] = (),
    store: StateStore | None = None,
) -> MatchResult | None:
    located = _locate_in_release(provider, snap, release_id, store)
    if located is None:
        return None
    release, track, method, fields, extra = located
    length_ms = track.get("length")
    found = (
        float(length_ms) / 1000.0
        if isinstance(length_ms, (int, float)) and length_ms
        else None
    )
    delta = _duration_delta(found, snap.duration)
    confidence = base_confidence * _locate_confidence(method, delta)
    extra = dict(extra)
    extra["定位"] = {
        "release_track_id": "发行曲目 MBID",
        "recording_id": "录音 MBID",
    }.get(method, "标题与时长")
    return MatchResult(
        source=source,
        confidence=round(min(1.0, confidence), 4),
        release_id=release_id,
        recording_id=str((track.get("recording") or {}).get("id") or "") if isinstance(track.get("recording"), dict) else "",
        album=str(release.get("title") or ""),
        duration_delta=delta,
        fields=fields,
        extra=extra,
        notes=list(notes),
    )


def _release_kinds(release: Mapping[str, object]) -> frozenset[str]:
    rg = release.get("release-group")
    rg = rg if isinstance(rg, dict) else {}
    kinds: set[str] = set()
    if rg.get("primary-type"):
        kinds.add(str(rg["primary-type"]).casefold())
    for item in rg.get("secondary-types") or []:
        if str(item).strip():
            kinds.add(str(item).casefold())
    return frozenset(kinds)


#: 精选/现场/原声这类发行在「目录名更像普通专辑」时降权。
#: 同一首歌在精选集里也有，不代表这个文件夹属于精选集。
_KIND_PENALTY = {
    "compilation": 0.26, "live": 0.20, "soundtrack": 0.14,
    "dj-mix": 0.22, "mixtape": 0.18, "demo": 0.12,
}


def _release_track_count(release: Mapping[str, object]) -> int:
    total = 0
    for medium in release.get("media") or []:
        if not isinstance(medium, dict):
            continue
        count = medium.get("track-count")
        if isinstance(count, int) and count > 0:
            total += count
        else:
            total += len(_medium_tracks_safe(medium))
    return total


def _medium_tracks_safe(medium: Mapping[str, object]) -> list[dict]:
    # 与 tagsources._medium_tracks 同义；这里只需要数量，避免再导私有函数。
    for key in ("tracks", "track"):
        value = medium.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return []


def _anchor_for_release(
    provider: MusicBrainzProvider,
    group: Sequence[TrackSnapshot],
    hints: _GroupHints,
    release_id: str,
    *,
    dominant: bool,
) -> FolderAnchor | None:
    release = provider.release_detail(release_id)
    if release is None:
        return None
    located = 0
    for snap in group:
        track, _medium, _method = locate_release_track(
            release,
            release_track_id=snap.release_track_id,
            recording_id=snap.recording_id,
            title=snap.title,
            duration=snap.duration,
            track_number=snap.track_number,
            disc_number=snap.disc_number,
        )
        if track is not None:
            located += 1
    coverage = located / max(1, len(group))
    if coverage < (1.0 if len(group) == 1 else 0.5):
        return None

    title = str(release.get("title") or "")
    name_sim = _sim(hints.album or group[0].album, title) if (hints.album or group[0].album) else 0.5
    total_tracks = _release_track_count(release)
    if total_tracks > 0:
        count_sim = 1.0 - min(
            1.0, abs(total_tracks - len(group)) / max(total_tracks, len(group))
        )
    else:
        count_sim = 0.5
    kinds = _release_kinds(release)
    penalty = 0.0
    if name_sim < 0.97:
        for kind, value in _KIND_PENALTY.items():
            if kind in kinds:
                penalty = max(penalty, value)
    score = (
        0.40 * coverage
        + 0.35 * name_sim
        + 0.10 * count_sim
        + (0.15 if dominant else 0.0)
        - penalty
    )
    trusted = False
    if len(group) == 1:
        # 单曲目录没有「簇」的证据，锚点只是它自己的 MBID；
        # 不据此覆盖任何已有值。
        trusted = False
    elif dominant and coverage >= 0.5 or coverage >= 0.7 and score >= 0.78:
        trusted = True
    notes: list[str] = []
    if dominant and name_sim < 0.97 and (kinds & set(_KIND_PENALTY)):
        notes.append("目录名更像普通专辑，未按精选/现场发行归类")
    return FolderAnchor(
        release_id=release_id,
        release=release,
        coverage=round(coverage, 4),
        score=round(score, 4),
        method="mbid" if dominant else "search",
        trusted=trusted,
        kinds=kinds,
        notes=notes,
    )


def _folder_anchor(
    provider: MusicBrainzProvider,
    group: Sequence[TrackSnapshot],
    hints: _GroupHints,
) -> FolderAnchor | None:
    """给一个目录选专辑锚点。

    候选来自：同目录多数 MBID、同发行组的不同版次、按目录名搜索的结果。
    评分同时看「能解释多少首本地曲目」「目录名像不像」「曲目数是否接近」，
    并对精选/现场/原声做降权——它们经常只是同一首歌的另一个出处，
    不该把整个目录改归到精选集。
    """
    candidates: list[tuple[str, bool]] = []
    if hints.dominant_album_id:
        # strict majority 才给「多数 MBID」加分；两首各指向一张时不偏袒。
        strict = hints.album_ids.get(hints.dominant_album_id, 0) * 2 > len(group)
        candidates.append((hints.dominant_album_id, strict))
    # 混有多个版次时，把每个出现过的 MBID 都当候选
    for release_id, _count in hints.album_ids.items():
        if release_id:
            candidates.append((release_id, False))
    # 同一发行组的不同版次：优先选曲目覆盖高的那张
    rg_counts = Counter(
        snap.tags.get("MUSICBRAINZ_RELEASEGROUPID", [""])[0]
        for snap in group
        if snap.tags.get("MUSICBRAINZ_RELEASEGROUPID")
    )
    for rg_id, count in rg_counts.most_common(2):
        if not rg_id or count * 2 < len(group):
            continue
        detail = provider.release_group_detail(rg_id)
        if detail is None:
            continue
        for release in (detail.get("releases") or [])[:6]:
            if isinstance(release, dict) and release.get("id"):
                candidates.append((str(release["id"]), False))
    # 整目录都没有 MBID，或目录名能搜到更合适的正常发行
    if hints.album:
        for release in provider.search_releases(
            album=hints.album, artist=hints.artist, limit=8
        ):
            candidates.append((str(release["id"]), False))

    seen: set[str] = set()
    anchors: list[FolderAnchor] = []
    for release_id, dominant in candidates:
        if not release_id or release_id in seen:
            continue
        seen.add(release_id)
        anchor = _anchor_for_release(
            provider, group, hints, release_id, dominant=dominant
        )
        if anchor is not None:
            anchors.append(anchor)
    if not anchors:
        return None
    anchors.sort(key=lambda item: (-item.score, -item.coverage))
    best = anchors[0]
    if hints.dominant_album_id and best.release_id != hints.dominant_album_id:
        best.notes.append("同目录多数 MBID 指向另一张发行，按整目录匹配改选")
    return best if best.trusted or best.coverage >= 0.7 else None


def _anchor_content_strength(
    snap: TrackSnapshot,
    track: Mapping[str, object],
    method: str,
) -> tuple[float, str]:
    """判断「锚点里的这一条」和本地文件是不是同一录音。

    录音 ID 相同是最强证据；没有录音 ID 时才看标题、时长与版本标记。
    本地录音 ID 与锚点不同时**直接否决**——这正是「现场版放进录音室
    专辑」要挡住的情况。
    """
    if method in ("release_track_id", "recording_id"):
        return 1.0, "同录音"
    recording = track.get("recording")
    recording = recording if isinstance(recording, dict) else {}
    anchor_recording = str(recording.get("id") or "")
    if snap.recording_id and anchor_recording:
        if snap.recording_id == anchor_recording:
            return 1.0, "同录音"
        return 0.0, "同目录里是另一条录音"
    if extract_version_tokens(snap.title) != extract_version_tokens(
        str(track.get("title") or recording.get("title") or "")
    ):
        return 0.0, "版本标记不一致"
    title_sim = _sim(snap.title, str(track.get("title") or ""))
    length_ms = track.get("length")
    delta = None
    if isinstance(length_ms, (int, float)) and length_ms and snap.duration:
        delta = abs(float(length_ms) / 1000.0 - float(snap.duration))
    if title_sim >= 0.99 and (delta is None or delta <= 2.0):
        return 0.96, "标题与时长一致"
    if title_sim >= 0.9 and (delta is None or delta <= 5.0):
        return 0.88, "标题与时长接近"
    if title_sim >= 0.82 and delta is not None and delta <= 3.0:
        return 0.8, "标题接近、时长吻合"
    return 0.0, "内容对不上"


def _match_from_anchor(
    provider: MusicBrainzProvider,
    snap: TrackSnapshot,
    anchor: FolderAnchor,
    store: StateStore | None,
) -> tuple[MatchResult | None, str]:
    located = _locate_in_release(provider, snap, anchor.release_id, store)
    if located is None:
        return None, "同目录专辑里没有找到这首"
    release, track, method, fields, extra = located
    strength, reason = _anchor_content_strength(snap, track, method)
    if strength < 0.85:
        return None, reason
    length_ms = track.get("length")
    delta = (
        abs(float(length_ms) / 1000.0 - float(snap.duration))
        if isinstance(length_ms, (int, float)) and length_ms and snap.duration
        else None
    )
    same_release = snap.album_id.casefold() == anchor.release_id.casefold()
    source = SOURCE_MBID if same_release else SOURCE_ALBUM
    notes = list(anchor.notes)
    if not same_release:
        notes.append("按同目录专辑统一发行信息")
    extra = dict(extra)
    extra["目录簇"] = (
        f"覆盖 {round(anchor.coverage * 100)}%"
        + (" · 可信" if anchor.trusted else "")
    )
    return (
        MatchResult(
            source=source,
            confidence=round(0.9 + 0.1 * strength, 4),
            release_id=anchor.release_id,
            recording_id=str(
                (track.get("recording") or {}).get("id") or ""
            ) if isinstance(track.get("recording"), dict) else "",
            album=str(release.get("title") or ""),
            duration_delta=delta,
            fields=fields,
            extra=extra,
            notes=notes,
            anchor_trusted=anchor.trusted,
            content_strength=strength,
        ),
        reason,
    )


def _mark_anchor_corrections(
    rows: Sequence[Mapping[str, object]], match: MatchResult
) -> list[dict]:
    """把可信目录簇下的发行级冲突改成「纠正」。

    只动 CORRECTABLE_FIELDS；标题、艺人、ISRC、人员、流派保持普通冲突。
    """
    out: list[dict] = []
    corrected = 0
    for row in rows:
        item = {
            "key": str(row["key"]),
            "current": list(row["current"]),
            "suggested": list(row["suggested"]),
            "action": str(row["action"]),
        }
        if (
            item["action"] == ACTION_CONFLICT
            and match.anchor_trusted
            and match.content_strength >= 0.95
            and item["key"] in CORRECTABLE_FIELDS
        ):
            item["action"] = ACTION_CORRECT
            corrected += 1
        out.append(item)
    if corrected:
        match.extra["按目录纠正"] = f"{corrected} 个发行字段"
        match.notes.append("本目录按同一张发行统一，发行信息以目录为准")
    return out


_PLACEHOLDER_ALBUMS = frozenset({
    "", "[standalone recordings]", "[unknown]", "[no album]", "unknown",
    "unknown album", "未知", "未知专辑", "未分类", "无专辑",
})


def _effective_album_hint(snap: TrackSnapshot, hints: _GroupHints) -> str:
    """本地真实专辑名优先；标签是占位时，退回目录名。

    目录名如果只是艺人名（常见于散放的 `孙露/趁早.flac`），不能当专辑。
    """
    if hints.album and not _album_is_weak([hints.album]):
        return hints.album
    folder = Path(snap.dir).name
    if not folder or _GENERIC_FOLDER.match(folder):
        return ""
    artist_names = {normalize_text(name) for name in snap.artists if name}
    if snap.albumartist:
        artist_names.add(normalize_text(snap.albumartist))
    if normalize_text(folder) in artist_names:
        return ""
    return folder


def _album_is_weak(values: Sequence[str]) -> bool:
    text = " ".join(str(v) for v in values).strip().casefold()
    return (
        not text
        or text in _PLACEHOLDER_ALBUMS
        or text.startswith("[")
    )


def _date_is_weak(values: Sequence[str]) -> bool:
    now_year = time.localtime().tm_year
    for value in values:
        match = re.match(r"^\s*(\d{4})", str(value))
        if match:
            year = int(match.group(1))
            if 1900 <= year <= now_year:
                return False
    return True


def _is_weak_local(
    key: str, values: Sequence[str], snap: TrackSnapshot
) -> bool:
    """本地这个值是不是「占位/明显无效」，而不是可信的已有信息。

    这些值不能当作「本地已有值」去阻止自动纠正：
      · `[standalone recordings]` 是 MusicBrainz 的伪发行占位；
      · 2027 这种未来日期不是有效发行日期；
      · 单曲录音 ID 指向一条没有发行的录音，不能代表专辑归属。
    """
    if key == "ALBUM":
        return _album_is_weak(values)
    if key == "DATE":
        return _date_is_weak(values)
    if key in (
        "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
        "MUSICBRAINZ_RELEASETRACKID", "MUSICBRAINZ_TRACKID",
        "TRACKNUMBER", "DISCNUMBER",
    ):
        if key in ("TRACKNUMBER", "DISCNUMBER"):
            return not [v for v in values if str(v).strip() and str(v).strip() != "0"]
        return _album_is_weak([snap.album])
    return False


_SEARCH_CORRECTABLE = CORRECTABLE_FIELDS | {"MUSICBRAINZ_TRACKID"}


_TITLE_NOISE = re.compile(r"[^\w]+", re.UNICODE)


def _title_equivalent(left: str, right: str) -> bool:
    """标题只差标点/空格，且版本标记一致，才算同一个名字。"""
    if extract_version_tokens(left) != extract_version_tokens(right):
        return False
    a = _TITLE_NOISE.sub("", normalize_text(left))
    b = _TITLE_NOISE.sub("", normalize_text(right))
    return bool(a) and a == b


def _artist_names_close(field: str, left: Sequence[str], right: Sequence[str]) -> bool:
    """两组艺人名逐项接近时的写法纠正，不用于人数不同的情况。"""
    a = _artist_tokens(field, left)
    b = _artist_tokens(field, right)
    if not a or not b or len(a) != len(b):
        return False
    return all(any(_sim(x, y) >= 0.86 for y in b) for x in a)


def _strong_evidence(match: MatchResult) -> bool:
    """是否已经确认「就是这条录音」，可以作为采纳远程署名的依据。"""
    if match.source == SOURCE_MBID:
        return match.confidence >= 0.99
    if match.source == SOURCE_ALBUM:
        return match.anchor_trusted and match.content_strength >= 0.95
    if match.source in (SOURCE_SEARCH, SOURCE_FINGERPRINT):
        return match.verified
    return False


def _auto_resolve_conflicts(
    rows: Sequence[Mapping[str, object]],
    match: MatchResult,
    snap: TrackSnapshot,
) -> list[dict]:
    """对已确认同一录音的冲突，按「远程更权威」自动采纳。

    只处理三类可以确定的差异：

    - 发行级字段：MBID 精确时远程就是该文件所属发行；
    - 标题：只差标点/空格，版本标记相同；
    - 艺人：同一艺人 ID、别名、只缺成员，或本地把作曲家误写成艺术家。

    人数不同、名字明显不同、版本不同的仍然留在复核。
    """
    if not _strong_evidence(match):
        return [
            {
                "key": str(row["key"]),
                "current": list(row["current"]),
                "suggested": list(row["suggested"]),
                "action": str(row["action"]),
            }
            for row in rows
        ]

    by_key = {
        str(row["key"]): row for row in rows if isinstance(row, Mapping)
    }

    def values(key: str, side: str) -> list[str]:
        row = by_key.get(key)
        return [str(v) for v in (row.get(side) or [])] if row else []

    local_ids = set(values("MUSICBRAINZ_ARTISTID", "current"))
    remote_ids = set(values("MUSICBRAINZ_ARTISTID", "suggested"))
    ids_ok = bool(local_ids) and (
        local_ids == remote_ids or local_ids < remote_ids
    )

    album_current = values("ALBUM", "current")
    album_suggested = values("ALBUM", "suggested")
    album_mismatch = bool(
        album_current
        and album_suggested
        and not _album_is_weak(album_current)
        and _sim(" ".join(album_current), " ".join(album_suggested)) < 0.8
    )

    artists_local = values("ARTIST", "current") or values("ARTISTS", "current")
    artists_remote = values("ARTIST", "suggested") or values("ARTISTS", "suggested")
    names_close = _artist_names_close(
        "ARTIST", artists_local, artists_remote
    ) or _artist_names_close("ARTISTS", artists_local, artists_remote)

    composer = {normalize_text(v) for v in (snap.tags.get("COMPOSER") or [])}
    local_artist = " ".join(artists_local).strip()
    role_fix = bool(composer) and normalize_text(local_artist) in composer

    albumartist = _first(snap.tags, "ALBUMARTIST") or _first(snap.tags, "ALBUM ARTIST")
    local_tokens = _artist_tokens("ARTIST", artists_local)
    album_tokens = _artist_tokens("ARTIST", [albumartist]) if albumartist else set()
    generic_album_artist = (
        len(local_tokens) == 1
        and local_tokens == album_tokens
        and normalize_text(local_artist) != normalize_text(" ".join(artists_remote))
    )

    artist_rule = ids_ok or names_close or role_fix or generic_album_artist
    sort_close = _artist_names_close(
        "ARTISTSORT", values("ARTISTSORT", "current"), values("ARTISTSORT", "suggested")
    )

    out: list[dict] = []
    adopted = 0
    for row in rows:
        item = {
            "key": str(row["key"]),
            "current": list(row["current"]),
            "suggested": list(row["suggested"]),
            "action": str(row["action"]),
        }
        if item["action"] != ACTION_CONFLICT:
            out.append(item)
            continue
        key = item["key"]
        adopt = False
        if (
            key in CORRECTABLE_FIELDS
            and match.source == SOURCE_MBID
            and not album_mismatch
        ):
            adopt = True
        elif key == "TITLE":
            adopt = _title_equivalent(
                " ".join(item["current"]), " ".join(item["suggested"])
            )
        elif key in ("ARTIST", "ARTISTS"):
            adopt = artist_rule
        elif key == "ARTISTSORT":
            adopt = artist_rule or sort_close
        elif key == "MUSICBRAINZ_ARTISTID":
            adopt = artist_rule
        if adopt:
            item["action"] = ACTION_CORRECT
            adopted += 1
        out.append(item)
    if adopted:
        match.extra["自动采纳"] = f"{adopted} 个署名/发行字段"
        match.notes.append("同一录音已确认，远程署名与发行信息更完整")
    return out


def _mark_search_corrections(
    rows: Sequence[Mapping[str, object]],
    match: MatchResult,
    snap: TrackSnapshot,
) -> list[dict]:
    """核验通过的搜索候选，可以纠正本地占位/无效的发行字段。

    只在标题、艺人、时长都吻合时 `verified=True`；本地值必须是占位或
    明显无效才会被改。真实的专辑名/日期即使不同也仍然进人工复核，
    因为那可能是另一个版次，不能替人决定。
    """
    out: list[dict] = []
    corrected = 0
    for row in rows:
        item = {
            "key": str(row["key"]),
            "current": list(row["current"]),
            "suggested": list(row["suggested"]),
            "action": str(row["action"]),
        }
        if (
            item["action"] == ACTION_CONFLICT
            and match.verified
            and item["key"] in _SEARCH_CORRECTABLE
            and _is_weak_local(item["key"], item["current"], snap)
        ):
            item["action"] = ACTION_CORRECT
            corrected += 1
        out.append(item)
    if corrected:
        match.extra["按搜索纠正"] = f"{corrected} 个占位字段"
        match.notes.append("本地发行信息是占位或无效值，已按核验通过的候选纠正")
    return out


def _recording_candidates(
    provider: MusicBrainzProvider, snap: TrackSnapshot
) -> list:
    """标题 + 艺人搜索，返回 MusicBrainz 的录音候选。"""
    try:
        return provider.lookup(
            title=snap.title,
            artists=snap.artists,
            release=snap.album,
            duration=snap.duration,
        )
    except Exception:  # noqa: BLE001 — 单曲查询失败不中断整批
        return []


def _candidate_summary(candidate) -> dict[str, object]:
    return {
        "provider_id": candidate.provider_id,
        "title": candidate.title,
        "artists": list(candidate.artists),
        "album": candidate.album,
        "duration": candidate.duration,
        "score": candidate.score,
        "duration_delta": None,
    }


def _search_match(
    provider: MusicBrainzProvider,
    snap: TrackSnapshot,
    hints: _GroupHints,
) -> tuple[MatchResult | None, list[dict[str, object]]]:
    """标题+艺人搜索，并用专辑名/时长交叉验证。"""
    candidates = _recording_candidates(provider, snap)
    summary = [_candidate_summary(c) for c in candidates[:5]]
    for item, candidate in zip(summary, candidates[:5], strict=False):
        item["duration_delta"] = _duration_delta(candidate.duration, snap.duration)

    ranked: list[tuple[float, object]] = []
    for candidate in candidates[:5]:
        title_score = _sim(snap.title, candidate.title)
        artist_score = max(
            (_sim(a, b) for a in snap.artists for b in candidate.artists),
            default=0.0,
        )
        delta = _duration_delta(candidate.duration, snap.duration)
        # 缺时长的候选不能被当成「完美吻合」：它恰恰是最需要核验的一类。
        duration_score = (
            0.2 if delta is None else max(0.0, 1.0 - delta / 10.0)
        )
        album_score = _sim(hints.album, candidate.album) if hints.album else 0.0
        score = (
            0.45 * title_score
            + 0.25 * artist_score
            + 0.20 * duration_score
            + 0.10 * album_score
        )
        if title_score < 0.82:
            continue
        if artist_score < 0.5:
            continue
        if delta is not None and delta > 8.0:
            continue
        ranked.append((score, candidate))
    ranked.sort(key=lambda pair: -pair[0])
    if not ranked:
        return None, summary

    best_score, best = ranked[0]
    ambiguous = len(ranked) > 1 and best_score - ranked[1][0] < 0.06
    album_hint = _effective_album_hint(snap, hints)

    # 用录音 ID 展开它所在的发行，选与本地专辑最接近的那张。
    try:
        link = EntityLink(
            entity="recording",
            mbid=best.provider_id,
            url=f"https://musicbrainz.org/recording/{best.provider_id}",
        )
        releases = lookup_by_link(
            provider,
            link,
            title=snap.title,
            duration=snap.duration,
            album=album_hint or snap.album,
            album_id=snap.album_id,
            recording_id=snap.recording_id,
            release_track_id=snap.release_track_id,
            max_releases=5,
        )
    except Exception:  # noqa: BLE001
        releases = []

    chosen = None
    for candidate in releases:
        fields = candidate.fields
        if "TRACKNUMBER" not in fields:
            continue
        release_album = str((fields.get("ALBUM") or [""])[0])
        name_match = bool(album_hint) and _sim(album_hint, release_album) >= 0.8
        id_match = bool(snap.album_id) and str(
            (fields.get("MUSICBRAINZ_ALBUMID") or [""])[0]
        ).casefold() == snap.album_id.casefold()
        if name_match or id_match or len(releases) == 1:
            chosen = candidate
            break
    if chosen is None and releases:
        chosen = next((c for c in releases if "TRACKNUMBER" in c.fields), None)
        ambiguous = True

    if chosen is None:
        return None, summary

    # 录音级时长经常比发行曲目更完整；伪发行/合辑常常缺 track length。
    # 只要能拿到其中一侧，就按它核验版本，不能因为发行缺 length 就当作
    # 「无法确认」而把整首推给人工。
    delta = _duration_delta(
        best.duration if best.duration is not None else chosen.duration,
        snap.duration,
    )
    title_ok = _sim(snap.title, chosen.title) >= 0.99
    artist_ok = max(
        (_sim(a, b) for a in snap.artists for b in chosen.artists),
        default=0.0,
    ) >= 0.85
    duration_ok = delta is not None and delta <= 3.0
    album_weak = not album_hint
    album_sim = _sim(album_hint, chosen.album) if album_hint else 0.0
    album_ok = album_sim >= 0.8
    # 没有专辑证据时，只有候选发行唯一才敢自动；多张发行时无从判断归属。
    album_evidence = album_ok or (album_weak and len(releases) == 1)
    verified = bool(
        title_ok and artist_ok and duration_ok and album_evidence and not ambiguous
    )

    confidence = best_score
    if verified:
        confidence = max(confidence, 0.97)
    if ambiguous:
        confidence -= 0.08
    extra = dict(chosen.extra)
    extra["匹配"] = "标题、艺人与时长一致" if verified else "标题艺人搜索"
    notes = [
        "标题、艺人、时长一致，可自动采用"
        if verified
        else "标题搜索来源，请逐项确认"
    ]
    if not duration_ok:
        notes.append(
            "候选缺时长，无法确认版本"
            if delta is None
            else f"候选时长差 {round(delta)} 秒，超出自动范围"
        )
    if not album_ok and not album_weak:
        notes.append("候选专辑与本地专辑不一致")
    if ambiguous:
        notes.append("多个候选分数接近，请确认是否同一版本")
    verification = (
        "标题、艺人、时长一致" if verified else "证据不足"
    )
    return (
        MatchResult(
            source=SOURCE_SEARCH,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            release_id=str((chosen.fields.get("MUSICBRAINZ_ALBUMID") or [""])[0]),
            recording_id=chosen.provider_id,
            album=chosen.album,
            duration_delta=delta,
            fields={k: list(v) for k, v in chosen.fields.items()},
            extra=extra,
            notes=notes,
            candidates=summary,
            content_strength=1.0 if verified else 0.7,
            verified=verified,
            verification=verification,
        ),
        summary,
    )

def _fingerprint_match(
    store: StateStore,
    root: RootConfig,
    snap: TrackSnapshot,
    hints: _GroupHints,
    *,
    provider: MusicBrainzProvider,
    fingerprinter: FpcalcFingerprinter | None,
    acoustid: AcoustIdClient | None,
    summary: TagScanSummary,
) -> MatchResult | None:
    """AcoustID 指纹 → 录音 ID → 发行定位。

    指纹与结果都缓存；文件大小与 mtime 没变就直接复用，
    避免每天扫描都把全库解码一遍。
    """
    if fingerprinter is None or acoustid is None:
        return None
    if not (fingerprinter.available and acoustid.available):
        return None

    matches: list = []
    cached = store.cached_fingerprint(
        snap.location_id, file_size=snap.file_size, file_mtime=snap.file_mtime
    )
    if cached is not None:
        try:
            raw = cached["result_json"]
            if raw:
                import json

                matches = [
                    _match_from_json(item)
                    for item in json.loads(str(raw))
                    if isinstance(item, dict)
                ]
        except Exception:  # noqa: BLE001 — 缓存坏了就重新查
            matches = []

    if not matches:
        try:
            fingerprint = fingerprinter.compute(snap.path)
            summary.fingerprints += 1
        except FingerprintError as exc:
            summary.errors.append(f"{snap.rel_path}: 指纹失败 {exc}")
            return None
        try:
            matches = list(acoustid.lookup(fingerprint.value, fingerprint.duration))
        except Exception as exc:  # noqa: BLE001 — 网络/限流失败只丢这一首的指纹
            summary.errors.append(f"{snap.rel_path}: AcoustID 查询失败 {exc}")
            matches = []
        import json

        store.save_fingerprint(
            location_id=snap.location_id,
            file_size=snap.file_size,
            file_mtime=snap.file_mtime,
            fingerprint=fingerprint.value,
            duration=fingerprint.duration,
            result_json=json.dumps([m.to_dict() for m in matches], ensure_ascii=False),
        )

    album_hint = _effective_album_hint(snap, hints)
    strong = [m for m in matches if m.score >= 0.8]
    for match in strong[:3]:
        for recording_id in match.recording_ids[:3]:
            try:
                releases = lookup_by_link(
                    provider,
                    EntityLink(
                        entity="recording",
                        mbid=recording_id,
                        url=f"https://musicbrainz.org/recording/{recording_id}",
                    ),
                    title=snap.title,
                    duration=snap.duration,
                    album=hints.album or snap.album,
                    album_id=snap.album_id,
                    recording_id=recording_id,
                    release_track_id=snap.release_track_id,
                    max_releases=5,
                )
            except Exception:  # noqa: BLE001
                continue
            for candidate in releases:
                if "TRACKNUMBER" not in candidate.fields:
                    continue
                release_album = str((candidate.fields.get("ALBUM") or [""])[0])
                album_score = _sim(album_hint, release_album) if album_hint else 0.0
                if album_score < 0.8 and len(releases) > 1:
                    continue
                delta = _duration_delta(candidate.duration, snap.duration)
                if delta is not None and delta > 10.0:
                    continue
                # 指纹只证明「录音是哪一条」，不证明「它属于哪张发行」。
                # 专辑名对不上、且有多张发行可选时降到复核线以下。
                verified = album_score >= 0.8 or (
                    not album_hint and len(releases) == 1
                )
                confidence = match.score * (1.0 if verified else 0.85)
                extra = dict(candidate.extra)
                extra["匹配"] = f"AcoustID 相似度 {match.score:.2f}"
                return MatchResult(
                    source=SOURCE_FINGERPRINT,
                    confidence=round(min(1.0, confidence), 4),
                    release_id=str(
                        (candidate.fields.get("MUSICBRAINZ_ALBUMID") or [""])[0]
                    ),
                    recording_id=recording_id,
                    album=candidate.album,
                    duration_delta=delta,
                    fields={k: list(v) for k, v in candidate.fields.items()},
                    extra=extra,
                    notes=["由音频指纹确认"],
                    content_strength=match.score,
                    verified=verified,
                    verification="指纹与专辑一致" if verified else "指纹确认录音，发行待确认",
                )
    return None


def _match_from_json(item: Mapping[str, object]):
    from .fingerprint import AcoustIdMatch

    return AcoustIdMatch(
        score=float(item.get("score") or 0),
        recording_ids=tuple(str(x) for x in (item.get("recording_ids") or [])),
        release_group_ids=tuple(str(x) for x in (item.get("release_group_ids") or [])),
        title=str(item.get("title") or ""),
        artists=tuple(str(x) for x in (item.get("artists") or [])),
    )


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def _decide_state(
    snap: TrackSnapshot,
    match: MatchResult,
    rows: Sequence[Mapping[str, object]],
    cfg: Config,
) -> str:
    actions = {str(row["action"]) for row in rows}
    has_change = bool(actions & {ACTION_FILL, ACTION_CORRECT})
    has_conflict = ACTION_CONFLICT in actions
    if not has_change and not has_conflict:
        return "satisfied"
    if not snap.writable:
        return STATE_REVIEW
    if has_conflict:
        return STATE_REVIEW
    if match.confidence < AUTO_MIN_CONFIDENCE:
        return STATE_REVIEW
    delta = match.duration_delta
    # MBID 是身份，不是相似度：同一录音可能因母带/压制不同差几秒，
    # 这不代表「不是它」。时长闸门只用于搜索、目录推定与指纹这些推断路径。
    if (
        delta is not None
        and delta > cfg.policy.tags.max_duration_delta_seconds
        and match.source != SOURCE_MBID
        and not (match.anchor_trusted and match.content_strength >= 0.95)
    ):
        return STATE_REVIEW
    # 标题搜索只有在标题、艺人、时长都吻合，且专辑不矛盾时才允许自动；
    # 缺时长或时长差超过 3 秒的一律人工确认。名字相似本身不足以证明身份。
    if match.source == SOURCE_SEARCH and not match.verified:
        return STATE_REVIEW
    return STATE_AUTO


def _payload(
    snap: TrackSnapshot,
    match: MatchResult | None,
    rows: Sequence[Mapping[str, object]],
    *,
    candidates: Sequence[Mapping[str, object]] = (),
    notes: Sequence[str] = (),
) -> dict[str, object]:
    current = {
        str(row["key"]): list(row["current"]) for row in rows if row["current"]
    }
    candidate = {
        str(row["key"]): list(row["suggested"]) for row in rows if row["suggested"]
    }
    return {
        "track": snap.track_dict(),
        "rows": [
            {
                "key": str(row["key"]),
                "current": list(row["current"]),
                "suggested": list(row["suggested"]),
                "action": str(row["action"]),
            }
            for row in rows
        ],
        "current": current,
        "candidate": candidate,
        "summary": summarise(rows),
        "extra": dict(match.extra) if match else {},
        "notes": list(dict.fromkeys([*notes, *(match.notes if match else [])])),
        "candidates": list(candidates),
        "writable": snap.writable,
        "match": {
            "source": match.source if match else "",
            "source_label": SOURCE_LABELS.get(match.source, "") if match else "",
            "confidence": match.confidence if match else 0.0,
            "release_id": match.release_id if match else "",
            "recording_id": match.recording_id if match else "",
            "album": match.album if match else "",
            "duration_delta": match.duration_delta if match else None,
            "verified": match.verified if match else False,
            "verification": match.verification if match else "",
        },
    }


def _write_auto(
    store: StateStore,
    cfg: Config,
    root: RootConfig,
    proposal_ids: Sequence[int],
    summary: TagScanSummary,
    progress: Callable[[str], None],
    *,
    review_safe_only: bool = False,
) -> None:
    """自动写入。逐文件隔离失败：一首出错不能毁掉整批。

    `review_safe_only=True` 用于「待审查里也有安全的补空/纠正」：
    只写这些行，冲突原样留下。标题搜索只有在时长/指纹核验通过时才参与；
    未核验的搜索候选可能整条都错，跳过。
    """
    from .tagwrite import apply_proposal

    limit = int(cfg.policy.tags.max_auto_write_per_run)
    if limit <= 0:
        # 0 表示不限制；关掉自动写入请用 auto_write=false，而不是上限 0。
        limit = len(proposal_ids)
    # 自动写入与监控/人工写入共用一把租约。扫描本身只读，可以慢慢跑；
    # 真正落盘前取租约，拿不到就跳过本轮（下一轮还会再扫到）。
    owner = f"tags-auto-{uuid.uuid4().hex[:8]}"
    # 先登记「有人在等」，监控会在写完当前文件后让出；最多等 90 秒。
    # 扫描本身已经跑完，不能因为监控恰好占着租约就丢掉整批写入。
    store.request_write(owner)
    try:
        acquired = store.acquire_write_lock(
            "write", owner, ttl_seconds=1800, wait_seconds=90.0
        )
    finally:
        store.cancel_write_request(owner)
    if not acquired:
        holder = store.write_lock_holder("write")
        summary.errors.append(f"另一个写入任务正在进行（{holder}），本轮未自动写入")
        return
    try:
        for index, proposal_id in enumerate(proposal_ids[:limit], start=1):
            if store.waiting_writers():
                summary.errors.append("有人工写入在等，自动写入已让出")
                break
            row = store.tag_proposal(proposal_id)
            if row is None:
                continue
            if review_safe_only:
                try:
                    payload = json.loads(str(row["payload_json"]))
                    match = payload.get("match") or {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    str(match.get("source") or "") == SOURCE_SEARCH
                    and not match.get("verified")
                ):
                    continue
                if float(match.get("confidence") or 0) < AUTO_MIN_CONFIDENCE:
                    continue
            try:
                result = apply_proposal(
                    store,
                    cfg,
                    root,
                    row,
                    fields=None,
                    require_auto=not review_safe_only,
                )
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(
                    f"{row['rel_path']}: 写入失败 {type(exc).__name__}: {exc}"
                )
                continue
            if result.ok and result.applied:
                summary.written += 1
                summary.written_fields += len(result.applied)
            elif not result.ok:
                summary.errors.append(f"{row['rel_path']}: 写入失败 {result.error}")
            if index % 20 == 0:
                progress(
                    f"已自动写入 {index}/{min(len(proposal_ids), limit)} 首…"
                )
    finally:
        store.release_write_lock("write", owner)


def scan_tags(
    cfg: Config,
    root: RootConfig,
    store: StateStore,
    provider: MusicBrainzProvider,
    *,
    auto_write: bool | None = None,
    limit: int | None = None,
    only_rel_paths: set[str] | None = None,
    progress: Callable[[str], None] = lambda _m: None,
    fingerprinter: FpcalcFingerprinter | None = None,
    acoustid: AcoustIdClient | None = None,
    refresh_skipped: bool = False,
) -> TagScanSummary:
    """扫描受管根并落库提案。`auto_write` 为真时顺手写入可自动的那部分。"""
    started = time.monotonic()
    summary = TagScanSummary()
    auto_write = cfg.policy.tags.auto_write if auto_write is None else auto_write

    if not provider.available or not provider.health_check():
        # 来源不可用时**绝不**继续扫描：否则每首歌都会变成「未匹配」，
        # 把上一轮辛苦得到的复核队列整体覆盖掉。
        summary.errors.append("MusicBrainz 不可用，未改动任何提案")
        summary.elapsed_seconds = time.monotonic() - started
        return summary

    audit = audit_root(root, only_rel_paths=only_rel_paths)
    from .pipeline import sync_state

    sync_state(store, cfg, root, audit)
    root_id = store.get_root_id(root.name)
    if root_id is None:
        summary.errors.append(f"受管根 {root.name} 未登记")
        summary.elapsed_seconds = time.monotonic() - started
        return summary

    locations = {
        str(row["rel_path"]): int(row["id"])
        for row in store.iter_locations(root_id)
        if row["missing_since"] is None
    }

    snapshots: list[TrackSnapshot] = []
    for track in audit.tracks:
        if limit is not None and len(snapshots) >= max(0, int(limit)):
            break
        info = track.audio
        rel = info.rel_path
        if only_rel_paths is not None and rel not in only_rel_paths:
            continue
        location_id = locations.get(rel)
        if location_id is None:
            continue
        try:
            tags, error = read_all_tags(info.path)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        if error:
            summary.errors.append(f"{rel}: {error}")
            continue
        media_format = (
            info.media_format.value
            if isinstance(info.media_format, MediaFormat)
            else str(info.media_format)
        )
        snapshots.append(
            snapshot_for(
                root,
                location_id,
                rel,
                info.path,
                tags,
                info.duration,
                media_format,
                int(info.size or 0),
                int(info.mtime or 0),
            )
        )
        summary.scanned += 1
        if summary.scanned % 50 == 0:
            progress(f"已读取 {summary.scanned} 首…")

    if not snapshots:
        summary.elapsed_seconds = time.monotonic() - started
        return summary

    groups: dict[str, list[TrackSnapshot]] = defaultdict(list)
    for snap in snapshots:
        groups[snap.dir].append(snap)

    auto_ids: list[int] = []
    review_ids: list[int] = []
    processed = 0
    total = len(snapshots)
    for folder in sorted(groups):
        group = groups[folder]
        parent_name = Path(folder).parent.name if folder not in ("", ".") else ""
        # **目录先成为一个专辑簇**：选一个能解释整目录的锚点发行。
        # 逐首再核验内容是否真是同一录音；内容不符才退回文件自己的证据。
        anchor_hints = _hints(group, group[0], parent_name)
        # 单曲目录没有簇证据，也不做跨发行纠正；直接走文件自身路径，
        # 省掉一次必然用不上的专辑搜索。
        anchor = (
            None if len(group) == 1
            else _folder_anchor(provider, group, anchor_hints)
        )
        for snap in group:
            processed += 1
            if processed % 20 == 0:
                progress(f"已匹配 {processed}/{total} 首…")
            hints = _hints(group, snap, parent_name)
            match: MatchResult | None = None
            candidates: list[dict[str, object]] = []
            rows: list[dict] | None = None
            anchor_reason = ""

            # 1) 可信目录锚点：发行级字段以目录为准
            if anchor is not None and anchor.trusted:
                match, anchor_reason = _match_from_anchor(provider, snap, anchor, store)
                if match is not None:
                    rows = _mark_anchor_corrections(
                        compare_fields(snap.tags, match.fields), match
                    )

            # 2) 锚点里没有这首，或内容不是同一录音：退回文件自己的 MBID
            if match is None and snap.album_id:
                match = _match_from_release(
                    provider, snap, snap.album_id,
                    source=SOURCE_MBID, base_confidence=1.0, store=store,
                )

            # 3) 没有锚点、也没有自己的 MBID：用同目录多数 MBID 补
            if match is None and not snap.album_id and hints.dominant_album_id:
                match = _match_from_release(
                    provider, snap, hints.dominant_album_id,
                    source=SOURCE_ALBUM, base_confidence=0.95,
                    notes=["同目录其它曲目指向这张发行"], store=store,
                )

            # 4) 标题 + 艺人搜索
            if match is None:
                match, candidates = _search_match(provider, snap, hints)

            # 5) 指纹兜底（用于确认内容，不用于推翻可信目录）
            if match is None or match.confidence < 0.92:
                fingerprinted = _fingerprint_match(
                    store, root, snap, hints,
                    provider=provider,
                    fingerprinter=fingerprinter,
                    acoustid=acoustid,
                    summary=summary,
                )
                if fingerprinted is not None and (
                    match is None or fingerprinted.confidence > match.confidence
                ):
                    match = fingerprinted

            if match is not None and anchor_reason:
                match.notes.append(anchor_reason)

            if match is None:
                summary.unresolved += 1
                store.upsert_tag_proposal(
                    location_id=snap.location_id,
                    state=STATE_UNMATCHED,
                    source=SOURCE_SEARCH,
                    confidence=0.0,
                    title=snap.title,
                    artists="; ".join(snap.artists),
                    album=snap.album,
                    payload=_payload(snap, None, [], candidates=candidates),
                    revive_skipped=refresh_skipped,
                )
                continue

            if rows is None:
                rows = compare_fields(snap.tags, match.fields)
            if match.source == SOURCE_SEARCH:
                rows = _mark_search_corrections(rows, match, snap)
            rows = _auto_resolve_conflicts(rows, match, snap)
            state = _decide_state(snap, match, rows, cfg)
            if state == "satisfied":
                summary.satisfied += 1
                existing = store.tag_proposal_for_location(snap.location_id)
                if existing is not None:
                    store.delete_tag_proposal(int(existing["id"]))
                continue

            if match.source == SOURCE_MBID:
                summary.matched_mbid += 1
            elif match.source == SOURCE_ALBUM:
                summary.matched_album += 1
            elif match.source == SOURCE_SEARCH:
                summary.matched_search += 1
            elif match.source == SOURCE_FINGERPRINT:
                summary.matched_fingerprint += 1

            payload = _payload(snap, match, rows, candidates=candidates)
            proposal_id = store.upsert_tag_proposal(
                location_id=snap.location_id,
                state=state,
                source=match.source,
                confidence=match.confidence,
                title=snap.title,
                artists="; ".join(snap.artists),
                album=match.album or snap.album,
                release_id=match.release_id,
                recording_id=match.recording_id,
                duration_delta=match.duration_delta,
                payload=payload,
                revive_skipped=refresh_skipped,
            )
            if state == STATE_AUTO:
                summary.auto += 1
                auto_ids.append(proposal_id)
            else:
                summary.review += 1
                review_ids.append(proposal_id)

    if auto_write and auto_ids:
        progress(f"正在自动写入 {min(len(auto_ids), cfg.policy.tags.max_auto_write_per_run)} 首…")
        _write_auto(store, cfg, root, auto_ids, summary, progress)
    if auto_write and review_ids:
        # 待审查里也有「补空 / 纠正」这类安全行；先把它们写掉，
        # 冲突仍留在复核队列里。搜索来源可能整条都错，跳过。
        progress("正在写入待审查里的安全字段…")
        _write_auto(
            store, cfg, root, review_ids, summary, progress,
            review_safe_only=True,
        )

    summary.elapsed_seconds = time.monotonic() - started
    progress(
        f"扫描完成：{summary.scanned} 首，可自动写入 {summary.auto}，"
        f"待审查 {summary.review}，未匹配 {summary.unresolved}"
    )
    return summary


def proposal_payload_from_match(
    snap: TrackSnapshot,
    match: MatchResult,
    *,
    source: str | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """按链接重新匹配后，生成新的 rows 与 payload。"""
    if source is not None:
        match.source = source
    rows = compare_fields(snap.tags, match.fields)
    return rows, _payload(snap, match, rows)
