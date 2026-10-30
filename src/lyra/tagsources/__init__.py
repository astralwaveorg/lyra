"""MusicBrainz 标签来源。

标签刮削以 MusicBrainz 为主源：曲库通常由 Picard 整理，已有标签的源头就是
MusicBrainz。QQ/网易云适合补歌词，不适合反向纠正 MusicBrainz 标签。

标签查询不复用歌词 Provider 协议：一次查询需要几十个字段，而 Provider.lyrics
只返回文本和时间轴。匹配层（评分、时长闸门、版本核验）仍然复用。

端点默认是 https://musicbrainz.org，也可以指向自建镜像。自建镜像通常以
容器名访问，需要把 Lyra 接入同一 Docker 网络。
"""

from __future__ import annotations

import difflib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..network import client_for

__all__ = [
    "EntityLink",
    "MbidLinkError",
    "MetadataCandidate",
    "MusicBrainzProvider",
    "MusicBrainzConfig",
    "compare_fields",
    "summarise",
    "lookup_by_release",
    "locate_release_track",
    "build_release_candidate",
    "lookup_by_link",
    "parse_entity_link",
]


@dataclass(slots=True)
class MusicBrainzConfig:
    base_url: str = ""
    """自建 MusicBrainz 的根地址。留空表示不可用（不退回公网，避免悄悄换源）。"""

    request_timeout_seconds: float = 20.0
    minimum_request_interval_seconds: float = 0.2
    search_limit: int = 8
    detail_limit: int = 3
    """只为前几条做详情查询。详情请求比搜索贵，全做会拖慢整批。"""


@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    """一个标签候选：某次录音的全部可写入字段。"""

    provider: str = "musicbrainz"
    provider_id: str = ""
    """录音 MBID。"""

    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    score: int = 0
    """MusicBrainz 自带的相似度（0-100）。它自己的排序依据，作为参考。"""

    fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """可直接写入的标签字段（键为大写）。`apply_tag_edit` 吃的就是这个。"""

    extra: Mapping[str, str] = field(default_factory=dict)
    """用于展示的补充信息：发行年份、发行方、封面地址等。"""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration": self.duration,
            "score": self.score,
            "fields": {k: list(v) for k, v in self.fields.items()},
            "extra": dict(self.extra),
        }


def _isrc_fields(payload: Mapping[str, object]) -> dict[str, list[str]]:
    """从响应里取出 ISRC 字段。

    兼容两种形态：字符串列表（MusicBrainz 实际返回的就是这个）
    与对象列表（部分镜像/旧版本会包一层 `{"id": ...}`）。
    """
    raw = payload.get("isrcs") or []
    codes: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            codes.append(item.strip())
        elif isinstance(item, dict) and item.get("id"):
            codes.append(str(item["id"]))
    return {"ISRC": codes} if codes else {}


#: 录音/作品关系类型 → 标签字段。只映射 Picard 也认的标准字段，
#: 不把「人声」「乐器」这类细碎关系塞进 PERFORMER（会让标签变成一堆人名）。
_CREDIT_FIELD = {
    "composer": "COMPOSER",
    "lyricist": "LYRICIST",
    "arranger": "ARRANGER",
    "producer": "PRODUCER",
    "engineer": "ENGINEER",
    "mix": "MIXER",
    "mastering": "ENGINEER",
    "conductor": "CONDUCTOR",
    "remixer": "REMIXER",
}

#: MusicBrainz 流派是**小写英文**，而曲库沿用的是 Picard/人工写法的首字母大写。
#: 写入时按已有习惯统一，避免同一风格出现「pop」与「Pop」两套。
_GENRE_LABELS = {
    "r&b": "R&B",
    "rnb": "R&B",
    "contemporary r&b": "Contemporary R&B",
    "hip hop": "Hip Hop",
    "pop rap": "Pop Rap",
    "pop rock": "Pop Rock",
    "dance-pop": "Dance-Pop",
    "synth-pop": "Synth-Pop",
    "k-pop": "K-Pop",
    "j-pop": "J-Pop",
    "cantopop": "Cantopop",
    "mandopop": "Mandopop",
    "singer-songwriter": "Singer-Songwriter",
    "edm": "EDM",
    "uk garage": "UK Garage",
    "lo-fi": "Lo-Fi",
    "drum and bass": "Drum and Bass",
}

#: 明显不是流派、只是使用者标签的词。MusicBrainz 的 tags 与 genres 混在一起，
#: 直接取会把「2000s」「female vocals」写进 GENRE。
_GENRE_DENY = {
    "albums i own", "bogus artist", "compilation", "female vocals", "male vocals",
    "favorite", "favorites", "seen live", "special purpose artist", "invalid isrcs",
    "many singles", "should be a series", "soundtrack", "score", "absolute",
}


def _genre_names(items: Sequence[object]) -> list[str]:
    """把 `genres` / `tags` 两种结构统一成按票数降序的名字列表。"""
    scored: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().casefold()
        if not name or name in _GENRE_DENY:
            continue
        if re.fullmatch(r"\d{2,4}s", name):   # 年代标签
            continue
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        scored.append((count, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _count, name in scored]


def _genre_label(name: str) -> str:
    key = name.strip().casefold()
    if key in _GENRE_LABELS:
        return _GENRE_LABELS[key]
    # 其余按「每个词首字母大写」显示；保留 & 与连字符的原样
    return " ".join(part[:1].upper() + part[1:] for part in key.split(" ")).strip()


def _artist_credit_details(
    credits: Sequence[object],
) -> tuple[list[str], str, list[str], list[str]]:
    """从 MusicBrainz 的 artist-credit 结构里取名字、拼接串、排序名与艺人 ID。

    artist-credit 既可能是 `[{"name": ..., "artist": {...}}]`，
    也可能夹着 `{"joinphrase": " & "}` 这种连接符——两者都要处理，
    否则合作曲目的艺人会丢。排序名（`sort-name`）是 Picard 的
    `ARTISTSORT`，缺失时不必编造。
    """
    names: list[str] = []
    sorts: list[str] = []
    ids: list[str] = []
    joined: list[str] = []
    for item in credits or ():
        if not isinstance(item, dict):
            continue
        if "joinphrase" in item and len(item) == 1:
            joined.append(str(item["joinphrase"]))
            continue
        artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
        name = str(artist.get("name") or item.get("name") or "").strip()
        if name:
            names.append(name)
            joined.append(name)
            sort = str(artist.get("sort-name") or "").strip()
            if sort:
                sorts.append(sort)
            if artist.get("id"):
                ids.append(str(artist["id"]))
        if item.get("joinphrase"):
            joined.append(str(item["joinphrase"]))
    return names, "".join(joined).strip(), sorts, ids


def _first_artist_credit(credits: Sequence[object]) -> tuple[list[str], str]:
    names, joined, _sorts, _ids = _artist_credit_details(credits)
    return names, joined


def _medium_tracks(medium: Mapping[str, object]) -> list[dict]:
    """取出一个 medium 里的曲目列表。

    MusicBrainz 有两个**看起来一样、实际不同**的形态：
    `media[].tracks`（查发行时，是列表）与 `media[].track`（查录音时，是单条）。
    只认前者的代码在「查录音」这条路上永远拿不到曲序与 release-track-id，
    而且不会报错——静默少字段，最难发现。
    """
    for key in ("tracks", "track"):
        value = medium.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return []


class MusicBrainzProvider:
    """MusicBrainz 标签查询。只读，不写入。"""

    name = "musicbrainz"

    def __init__(
        self,
        config: MusicBrainzConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_request = 0.0
        # 进程内缓存：一次全库扫描会反复查同一张发行（十几首共用一张），
        # 不缓存就会把同一份 JSON 拉十几遍。缓存只存**上游原始响应**，
        # 字段解析仍按当前代码走，升级解析逻辑不会读到旧结构。
        self._release_cache: dict[str, dict] = {}
        self._release_group_cache: dict[str, dict] = {}
        self._recording_cache: dict[str, dict] = {}
        self._work_cache: dict[str, dict] = {}
        self._artist_cache: dict[str, dict] = {}
        self._client = client_for(
            "musicbrainz",
            timeout=config.request_timeout_seconds,
            transport=transport,
            headers={
                # MusicBrainz 要求带上有意义的 UA，自建镜像同样遵守这个约定
                "User-Agent": f"Lyra/{__version__} (+https://github.com/astralwaveorg/lyra)",
                "Accept": "application/json",
            },
        )

    @property
    def available(self) -> bool:
        return bool(self._config.base_url.strip())

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: Mapping[str, str]) -> dict:
        """带最小间隔的 GET。自建镜像没有公网限流，但仍不该打爆它。"""
        wait = self._config.minimum_request_interval_seconds - (
            self._clock() - self._last_request
        )
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()
        url = self._config.base_url.rstrip("/") + path
        response = self._client.get(url, params=dict(params))
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _get_cached(
        self,
        cache: dict[str, dict],
        key: str,
        path: str,
        params: Mapping[str, str],
    ) -> dict:
        """同一进程内对同一实体只请求一次。

        缓存**不跨扫描**：上游数据会修订，长期缓存会把过期结果带进写入。
        一次扫描通常几十分钟内完成，这个窗口足够短。
        """
        if key in cache:
            return cache[key]
        data = self._get(path, params)
        cache[key] = data
        return data

    def health_check(self, *, retries: int = 3, backoff_seconds: float = 3.0) -> bool:
        """确认来源真的能应答，而不只是配置了地址。

        扫描前用它挡一次：镜像挂了/网络断了时，逐首查询会全部失败，
        整个库会被误标成「未匹配」，把上一轮的复核队列覆盖掉。

        **限流要退避重试，不能直接放弃。** 公共 MusicBrainz 限流是每秒
        一次请求，长扫描中随机 503 是常态（实测连续 5 次请求就有 1 次 503）。
        一次 503 就放弃整轮扫描，等于标签自动化随机停摆——这正是「自动化
        时好时坏」的来源之一。这里对 429/503 做有界退避重试；只有真正的
        连接错误才判定为不可用。
        """
        if not self.available:
            return False
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                self._get(
                    "/ws/2/recording",
                    {"query": "recording:*", "fmt": "json", "limit": "1"},
                )
                return True
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                if status not in (429, 503):
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                break
            # 限流/过载：退避后重试（等待时间逐次加倍）
            self._sleep(backoff_seconds * (attempt + 1))
        _ = last_error
        return False

    def release_detail(self, release_id: str) -> dict | None:
        """取一张发行的完整内容（含曲目、厂牌、发行组）。

        `inc` 里必须同时有 `media` 与 `recordings`：只有 media 时
        MusicBrainz 不返回曲目列表，曲序与 release-track-id 就永远取不到。
        """
        mbid = (release_id or "").strip()
        if not mbid or not self.available:
            return None
        try:
            return self._get_cached(
                self._release_cache,
                mbid,
                f"/ws/2/release/{mbid}",
                {
                    "fmt": "json",
                    # `isrcs` 让**整张发行的每条曲目**都带上 ISRC，
                    # 不必再为每首歌单独查一次录音（全库扫描能省上千次请求）。
                    "inc": "artist-credits+labels+release-groups+media+recordings+isrcs",
                },
            )
        except Exception:  # noqa: BLE001 — 查不到由调用方兜底
            return None

    def release_group_detail(self, release_group_id: str) -> dict | None:
        mbid = (release_group_id or "").strip()
        if not mbid or not self.available:
            return None
        try:
            return self._get_cached(
                self._release_group_cache,
                mbid,
                f"/ws/2/release-group/{mbid}",
                {"fmt": "json", "inc": "genres+tags+artist-credits+releases"},
            )
        except Exception:  # noqa: BLE001
            return None

    def recording_detail(self, recording_id: str) -> dict | None:
        mbid = (recording_id or "").strip()
        if not mbid or not self.available:
            return None
        try:
            return self._get_cached(
                self._recording_cache,
                mbid,
                f"/ws/2/recording/{mbid}",
                {
                    "fmt": "json",
                    "inc": "isrcs+artist-credits+releases+release-groups+work-rels"
                    "+artist-rels",
                },
            )
        except Exception:  # noqa: BLE001
            return None

    def work_detail(self, work_id: str) -> dict | None:
        mbid = (work_id or "").strip()
        if not mbid or not self.available:
            return None
        try:
            return self._get_cached(
                self._work_cache,
                mbid,
                f"/ws/2/work/{mbid}",
                {"fmt": "json", "inc": "artist-rels"},
            )
        except Exception:  # noqa: BLE001
            return None

    def artist_detail(self, artist_id: str) -> dict | None:
        mbid = (artist_id or "").strip()
        if not mbid or not self.available:
            return None
        try:
            return self._get_cached(
                self._artist_cache,
                mbid,
                f"/ws/2/artist/{mbid}",
                {"fmt": "json", "inc": "genres+tags"},
            )
        except Exception:  # noqa: BLE001
            return None

    def search_releases(
        self, *, album: str, artist: str = "", limit: int = 8
    ) -> list[dict]:
        """按专辑名（+ 艺人）搜索发行，用于「整个目录都没有 MBID」的兜底。

        专辑名与艺人都是**软条件**：本地标签可能是繁体、翻译或带后缀，
        硬 AND 会让明明存在的专辑查不到。只要求专辑名参与匹配，
        艺人用于排序加权。
        """
        title = (album or "").strip()
        if not title or not self.available:
            return []
        query = f'release:"{self._escape(title)}"'
        person = (artist or "").strip()
        if person:
            query += f' AND artist:"{self._escape(person)}"'
        try:
            payload = self._get(
                "/ws/2/release",
                {
                    "query": query,
                    "fmt": "json",
                    "limit": str(max(1, min(25, int(limit)))),
                },
            )
        except Exception:  # noqa: BLE001
            return []
        releases = payload.get("releases") or []
        return [r for r in releases if isinstance(r, dict) and r.get("id")]

    def genres_for_release(self, release: Mapping[str, object]) -> tuple[str, ...]:
        """取发行所属发行组的流派，缺失时退回艺人流派。

        实际华语发行的发行组经常一个流派都没有（曲库某张合辑就是），
        而艺人有 `mandopop`/`cantopop` 这类标注。两级都试，
        总比空着强；但**只取来源明确给出的**，不按曲风瞎猜。
        """
        rg = release.get("release-group")
        names: list[str] = []
        if isinstance(rg, dict) and rg.get("id"):
            detail = self.release_group_detail(str(rg["id"])) or {}
            names = _genre_names(detail.get("genres") or [])
            if not names:
                names = _genre_names(detail.get("tags") or [])
        if not names:
            for credit in release.get("artist-credit") or []:
                if not isinstance(credit, dict):
                    continue
                artist = credit.get("artist") if isinstance(credit.get("artist"), dict) else {}
                if not artist.get("id"):
                    continue
                detail = self.artist_detail(str(artist["id"])) or {}
                names = _genre_names(detail.get("genres") or [])
                if names:
                    break
        return tuple(_genre_label(n) for n in names[:3])

    def credits_for_recording(self, recording_id: str) -> dict[str, list[str]]:
        """录音/作品关系 → 作曲、作词、编曲、制作人、工程师、混音、演奏者。

        作曲与作词挂在**作品**上，不在录音上：录音的 `work-rels` 只给出
        「这条录音表演的是哪个作品」，还得再查一次作品。这一步比较贵，
        所以调用方应先查本地缓存（`recording_credits` 表）。
        """
        detail = self.recording_detail(recording_id)
        if detail is None:
            return {}
        out: dict[str, list[str]] = {}

        def add(key: str, name: object) -> None:
            text = str(name or "").strip()
            if text and text not in out.setdefault(key, []):
                out[key].append(text)

        for relation in detail.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            kind = str(relation.get("type") or "")
            artist = relation.get("artist")
            name = (artist or {}).get("name") if isinstance(artist, dict) else None
            if name:
                mapped = _CREDIT_FIELD.get(kind)
                if mapped:
                    add(mapped, name)

        # 作品关系：先收集作品 ID（最多 3 个，避免一条录音挂十几个作品时打爆上游）
        work_ids = []
        for relation in detail.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            work = relation.get("work")
            if isinstance(work, dict) and work.get("id"):
                work_ids.append(str(work["id"]))
        for work_id in list(dict.fromkeys(work_ids))[:3]:
            work = self.work_detail(work_id) or {}
            for relation in work.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                kind = str(relation.get("type") or "")
                mapped = _CREDIT_FIELD.get(kind)
                artist = relation.get("artist")
                if mapped and isinstance(artist, dict):
                    add(mapped, artist.get("name"))
        return out

    @staticmethod
    def _escape(value: str) -> str:
        """Lucene 查询语法里的保留字符必须转义，否则歌名里的括号会让查询报错。"""
        out = []
        for ch in value:
            if ch in r'+-&|!(){}[]^"~*?:\/':
                out.append("\\" + ch)
            else:
                out.append(ch)
        return "".join(out)

    def build_query(self, title: str, artists: Sequence[str]) -> str:
        """构造 Lucene 查询串。

        用 `recording:` 与 `artist:` 限定字段，比全文裸搜准确得多；
        合作曲目只取主艺人——实际把整串 `A & B` 塞进去会大幅降低命中。
        """
        parts = []
        if title.strip():
            parts.append(f'recording:"{self._escape(title.strip())}"')
        primary = next((a.strip() for a in artists if a and a.strip()), "")
        if primary:
            parts.append(f'artist:"{self._escape(primary)}"')
        return " AND ".join(parts) or "recording:*"

    def lookup(
        self,
        *,
        title: str,
        artists: Sequence[str] = (),
        release: str = "",
        duration: float | None = None,
    ) -> list[MetadataCandidate]:
        """按标题与艺人查候选，并为前几条补详情。"""
        if not self.available:
            return []

        # **有意不把专辑名 AND 进查询。**
        # 实际本地专辑名是 `18…`，而 MusicBrainz 上叫「棋開得勝」——
        # 作为硬过滤条件会让查询直接返回 0 条，看起来像「这个源查不到这首歌」。
        # 专辑名只用于**排序加权**：同名专辑的候选排前面。
        query = self.build_query(title, artists)

        payload = self._get(
            "/ws/2/recording",
            {"query": query, "fmt": "json", "limit": str(self._config.search_limit)},
        )
        recordings = payload.get("recordings") or []
        if not isinstance(recordings, list):
            return []

        candidates: list[MetadataCandidate] = []
        for index, item in enumerate(recordings):
            if not isinstance(item, dict):
                continue
            built = self._candidate_from(item, duration=duration)
            if built is None:
                continue
            # 前几条补详情：搜索接口不含 ISRC，也没有完整的发行信息
            if index < self._config.detail_limit and built.provider_id:
                built = self._enrich(built)
            candidates.append(built)

        wanted_album = release.strip().casefold()
        if wanted_album:
            # 专辑名只影响顺序，不影响是否命中
            candidates.sort(
                key=lambda c: (
                    0 if wanted_album and wanted_album in (c.album or "").casefold() else 1,
                    -c.score,
                )
            )
        return candidates

    def _candidate_from(
        self, item: Mapping[str, object], *, duration: float | None
    ) -> MetadataCandidate | None:
        mbid = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not mbid or not title:
            return None

        names, joined = _first_artist_credit(item.get("artist-credit") or [])
        length_ms = item.get("length")
        found_duration = (
            float(length_ms) / 1000.0
            if isinstance(length_ms, (int, float)) and length_ms
            else None
        )

        fields: dict[str, list[str]] = {"TITLE": [title]}
        if names:
            fields["ARTIST"] = [joined or "/".join(names)]
            # ARTISTS 是 Picard 为合作曲目写的多值字段，与 ARTIST 并存
            fields["ARTISTS"] = names
        if found_duration:
            fields["MUSICBRAINZ_TRACKID"] = [mbid]

        extra: dict[str, str] = {}
        try:
            score = int(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0

        return MetadataCandidate(
            provider_id=mbid,
            title=title,
            artists=tuple(names),
            duration=found_duration if found_duration is not None else duration,
            score=score,
            fields=fields,
            extra=extra,
        )

    def _enrich(self, candidate: MetadataCandidate) -> MetadataCandidate:
        """补详情：ISRC、发行信息、曲序、流派与演职员。

        搜索接口只给「录音出现在哪些发行上」的摘要，**不含曲序、厂牌、
        ISRC**。这里按录音 ID 再查一次，并挑一张带曲目表的发行走
        `lookup_by_release`——比在摘要上手工拼字段更不容易漏。
        """
        detail = self.recording_detail(candidate.provider_id)
        if detail is None:
            return candidate

        fields = {k: list(v) for k, v in candidate.fields.items()}
        extra = dict(candidate.extra)
        fields.update(_isrc_fields(detail))

        releases = [r for r in (detail.get("releases") or []) if isinstance(r, dict)]
        chosen = next(
            (
                rel for rel in releases
                if any(
                    isinstance(m, dict) and (m.get("track") or m.get("tracks"))
                    for m in (rel.get("media") or [])
                )
            ),
            None,
        )
        if chosen is not None and chosen.get("id"):
            built = lookup_by_release(
                self,
                release_id=str(chosen["id"]),
                recording_id=candidate.provider_id,
                title=candidate.title,
                duration=candidate.duration,
            )
            if built is not None:
                # 录音级字段（ISRC / TRACKID / ARTIST）以搜索结果为准，
                # 发行级字段用完整发行覆盖——两者合并而不是二选一。
                fields.update({k: list(v) for k, v in built.fields.items()})
                extra.update(built.extra)

        return MetadataCandidate(
            provider=candidate.provider,
            provider_id=candidate.provider_id,
            title=candidate.title,
            artists=candidate.artists,
            album=str((fields.get("ALBUM") or [""])[0]),
            duration=candidate.duration,
            score=candidate.score,
            fields=fields,
            extra=extra,
        )

# ---------------------------------------------------------------------------
# 字段级对比
# ---------------------------------------------------------------------------

#: 字段的处置方式。标签刮削**绝不能「一键覆盖全部」**——
#: 有些字段要覆盖，有些必须保留（例如用户自己填的 GENRE）。
ACTION_SAME = "same"
ACTION_FILL = "fill"
ACTION_CONFLICT = "conflict"
#: 按同目录专辑簇纠正：本地有值，但与本目录一致的发行不同。
#: 它**不是**普通冲突——来源是整个目录的共识，且内容已核验是同一录音；
#: 自动流程可以写，界面用「纠正」标出来。
ACTION_CORRECT = "correct"

_ACTION_ORDER = {
    ACTION_CONFLICT: 0, ACTION_CORRECT: 1, ACTION_FILL: 2, ACTION_SAME: 3,
}


_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "TOTALTRACKS": ("TRACKTOTAL",),
    "TRACKTOTAL": ("TOTALTRACKS",),
    "TOTALDISCS": ("DISCTOTAL",),
    "DISCTOTAL": ("TOTALDISCS",),
    "ARTIST": ("ARTISTS",),
    "ALBUMARTIST": ("ALBUMARTISTS",),
}

#: 多值、且顺序不影响含义的字段。顺序不同不该判成冲突。
_ORDER_FREE = frozenset({
    "ARTIST", "ARTISTS", "ALBUMARTIST", "ALBUMARTISTS",
    "LABEL", "ISRC", "COMPOSER", "LYRICIST",
    "ARRANGER", "PRODUCER", "ENGINEER", "MIXER", "CONDUCTOR", "REMIXER",
    "RELEASETYPE", "GENRE", "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID",
})

_NUMERIC_FIELDS = frozenset({
    "TRACKNUMBER", "DISCNUMBER", "TOTALTRACKS", "TRACKTOTAL",
    "TOTALDISCS", "DISCTOTAL",
})

_DATE_FIELDS = frozenset({"DATE", "ORIGINALDATE"})

#: 「人名类」字段：写的是**同一个人/同一组人**，只是写法不同时，
#: 不算冲突（简繁、全半角空格、「Beatles, The」vs「The Beatles」）。
_HUMAN_NAME_FIELDS = frozenset({
    "ARTIST", "ARTISTS", "ALBUMARTIST", "ALBUMARTISTS",
    "ARTISTSORT", "ALBUMARTISTSORT",
    "COMPOSER", "LYRICIST", "PRODUCER", "ARRANGER",
    "ENGINEER", "MIXER", "CONDUCTOR", "REMIXER",
})

#: 冠词倒置写法：「Beatles, The」与「The Beatles」是同一个艺人。
_INVERTIBLE_ARTICLE = re.compile(
    r"^(.+)\s*,\s*(the|a|an|los|las|le|la|les|el)\s*$", re.IGNORECASE
)

#: 专辑「版本括号」里的词：括号内容含这些词时，括号是**版本说明**
#: （再版/豪华/介质/限量），不是标题本身的一部分。
_EDITION_WORDS = re.compile(
    r"edition|version|ver\.|deluxe|remaster|reissue|expanded|anniversary|"
    r"deluxe\s+edition|special|limited|collector|target\s+exclusive|"
    r"cd\s*\d|disc\s*\d|bonus|explicit|clean|mono|stereo|"
    r"hi-?res|24\s*bit|96\s*khz|dsd|mfsl|vinyl|shm|sacd|super\s?vinyl|"
    r"版|豪华|特別|特别|紀念|纪念|限量|再版|復刻|复刻|預購|预购|首刷|盤|盘",
    re.IGNORECASE,
)

_EDITION_SUFFIX = re.compile(r"[\s]*[([\uFF08\u3010\u300C]([^)\]\uFF09\u3011\u300D]*)[)\]\uFF09\u3011\u300D]\s*$")

#: 专辑名的**裸碟后缀**（无括号）：`Red CD1`、`范特西 Disc 2`——
#: 分碟文件常把碟号写进专辑名，与基础名是同一张专辑。
_DISC_SUFFIX = re.compile(r"\s+(?:CD|Disc|Disk|碟)\s*\d{1,2}$", re.IGNORECASE)

#: 本地专辑/艺人字段的**占位值**：不是人工整理的痕迹，是「没填」。
_PLACEHOLDER_VALUES = frozenset({
    "[unknown]", "(unknown)", "unknown", "unknown album", "unknown artist",
    "未知专辑", "未知艺人", "未知", "未命名", "new album", "<unknown>",
})

#: 完整日期之间**一天的差**视为同一发行（时区/地区发行日不同）。
_DATE_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(text: str):
    from datetime import date

    try:
        parts = [int(x) for x in text.split("-")]
        if len(parts) == 3:
            return date(parts[0], parts[1], parts[2])
    except ValueError:
        return None
    return None


def _strip_edition(title: str) -> str:
    """去掉专辑名尾部的「版本括号」，返回基础名；无版本括号时原样返回。

    只剥**尾部**括号、且括号内容必须是版本词——`Red (Taylor's Version)`
    和 `Midnights [Jade Green Edition]` 是同一张专辑的具体版次，
    与来源的 `Red` / `Midnights` 是同一专辑，不该算冲突。
    括号里是「Live」「Deluxe」之外的内容（伴奏标记、别名）不动。
    """
    text = (title or "").strip()
    while True:
        m = _EDITION_SUFFIX.search(text)
        if m and _EDITION_WORDS.search(m.group(1)):
            text = text[: m.start()].strip()
            continue
        m = _DISC_SUFFIX.search(text)
        if m:
            text = text[: m.start()].strip()
            continue
        break
    return text


_ZH_NUMERALS = "零一二三四五六七八九"


def _zh_numeral_to_arabic(text: str) -> str:
    """中文数字（≤99 的常见形态）→ 阿拉伯数字，只用于比较。

    `十一月的蕭邦` 与 `11月的蕭邦` 是同一张专辑（数字写法不同）；
    折叠后仍不同会留成永远解决不了的冲突。只处理「十」「十一」
    「二十」「二十一」这类简单形态——复杂的（万、亿、两）不碰。
    """
    def repl(m: re.Match[str]) -> str:
        raw = m.group(0)
        if raw == "十":
            return "10"
        if "十" not in raw:
            return raw  # 一~九：单独出现时常是词的一部分，不动
        tens, ones = raw.split("十", 1)
        tens_n = _ZH_NUMERALS.index(tens) if tens else 1
        ones_n = _ZH_NUMERALS.index(ones) if ones else 0
        return str(tens_n * 10 + ones_n)

    return re.sub(
        r"[零一二三四五六七八九十]{1,3}(?=世纪|[月日年第张首辑号纪代])", repl, text
    )


def _article_canonical(text: str) -> str:
    """「beatles, the」→「the beatles」：冠词倒置归一（**只用于比较**）。"""
    m = _INVERTIBLE_ARTICLE.match(text.strip())
    if m:
        return f"{m.group(2)} {m.group(1)}"
    return text


def _fold_value(field: str, value: str) -> str:
    """字段的「比较形」：全半角/引号/简繁/大小写折叠；人名字段再
    归一冠词倒置并去掉全部空白（`G.E.M.鄧紫棋` 与 `G.E.M. 鄧紫棋`
    是同一个署名，只是少了个空格）。

    只用于**比较**，绝不写回文件。
    """
    from ..match import normalize_text

    text = normalize_text(value)
    text = _zh_numeral_to_arabic(text)
    if field in _HUMAN_NAME_FIELDS:
        text = re.sub(r"\s+", "", _article_canonical(text))
    return text


_ARTIST_SPLIT = re.compile(
    r"\s*(?:[、,，;；]|&|＋|\+|·|×|/|\bwith\b|\bfeat(?:uring)?\.?|\bft\.?)\s*",
    re.IGNORECASE,
)


def _split_values(field: str, values: Sequence[str]) -> list[str]:
    """把「一个值里塞了多个」拆开再比较。

    曲库大量 GENRE 是一条 `Ballad / Pop`、`A; B` 这类合并串；
    直接与来源的多值列表比会全部判成冲突。只在**比较时**拆，
    不改变写入内容。分隔符按字段区分：艺人名里的 `/` 可能是名字的一部分
    （AC/DC），只有流派和艺人字段才拆得这么激进。
    """
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if field == "GENRE":
            parts = re.split(r"\s*[;／/,]\s*", text) if len(text) < 200 else [text]
        elif field in ("ARTIST", "ALBUMARTIST", "ARTISTS", "ALBUMARTISTS"):
            if _INVERTIBLE_ARTICLE.match(text):
                # 「Beatles, The」是冠词倒置的**一个**艺人，不是「Beatles」
                # 和「The」两个人——先于逗号拆分识别。
                parts = [text]
            else:
                parts = _ARTIST_SPLIT.split(text) if len(text) < 200 else [text]
        elif field in _ORDER_FREE:
            parts = re.split(r"\s*[;]\s*", text)
        else:
            parts = [text]
        cleaned = [p.strip() for p in parts if p.strip()]
        out.extend(cleaned or [text])
    return out


def _norm_values(field: str, values: Sequence[str]) -> list[str]:
    if field in _NUMERIC_FIELDS:
        fixed = []
        for value in values:
            digits = str(value or "").strip().lstrip("0")
            fixed.append(digits or "0")
        return fixed
    return [_fold_value(field, v) for v in _split_values(field, values)]


_SORT_SEPARATORS = re.compile(
    r"[\s,;&、，＋+·]+|\bwith\b|\band\b|\bfeat(?:uring)?\.?",
    re.IGNORECASE,
)


def _sort_key(values: Sequence[str]) -> str:
    """排序名只看字符序列，不区分分隔符。

    曲库把多艺人排序名写成 `A, B & C`，而 MusicBrainz 写 `A; B; C`；
    两者语义完全一样，按字面比会制造大量假冲突。逗号在排序名里
    本身是「姓, 名」的一部分，所以**只删除分隔符、不拆分**。
    """
    joined = " ".join(str(v) for v in values)
    return re.sub(r"\s+", "", _SORT_SEPARATORS.sub("", joined)).casefold()


_EXTENDABLE = frozenset({
    "ARTIST", "ARTISTS", "ALBUMARTIST", "ALBUMARTISTS",
    "ARTISTSORT", "ALBUMARTISTSORT",
    "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID",
})

_SORT_ARTIST_SPLIT = re.compile(
    r"\s*(?:[;、，/·]|&|\bwith\b|\band\b|\bfeat(?:uring)?\.?)\s*",
    re.IGNORECASE,
)


def _artist_tokens(field: str, values: Sequence[str]) -> set[str]:
    """把艺人相关字段拆成「去重后的成员集合」，用于判断包含关系。"""
    out: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if field in ("ARTISTSORT", "ALBUMARTISTSORT"):
            parts = _SORT_ARTIST_SPLIT.split(text)
        elif field in ("ARTIST", "ARTISTS", "ALBUMARTIST", "ALBUMARTISTS"):
            parts = _ARTIST_SPLIT.split(text)
        else:  # MBID 列表
            parts = re.split(r"[\s;]+", text)
        for part in parts:
            token = part.strip().casefold()
            if token:
                out.add(token)
    return out


def _is_extension(field: str, now: Sequence[str], want: Sequence[str]) -> bool:
    """本地是来源的**真子集**：只缺成员，没有多出来的东西。

    典型场景：本地只写了主艺人，MusicBrainz 发行里还列了合作艺人。
    补上这些成员不会删除任何已有信息；反过来（本地多了来源没有的人）
    不算补齐，仍然进人工复核。
    """
    if field not in _EXTENDABLE:
        return False
    local = _artist_tokens(field, now)
    remote = _artist_tokens(field, want)
    if bool(local) and bool(remote) and local < remote:
        return True
    if field in _HUMAN_NAME_FIELDS:
        # 「G.E.M.」→「G.E.M. 鄧紫棋」：本地只有主艺人，来源补全了整组
        # 署名。去空白折叠后本地是来源的**前缀**（且不少于 3 个字符，
        # 排除单字截断）也算补齐——`_artist_tokens` 不拆空格，这种
        # 连写形式它看不见。
        local_join = "".join(_fold_value(field, v) for v in now)
        remote_join = "".join(_fold_value(field, v) for v in want)
        return (
            len(local_join) >= 3
            and bool(remote_join)
            and remote_join.startswith(local_join)
        )
    return False


def _same_value(field: str, now: Sequence[str], want: Sequence[str]) -> bool:
    """判断两边是否**语义等价**。

    等价不代表逐字相同：`01` 与 `1`、`2009` 与 `2009-09-09`、
    `pop` 与 `Pop` 都算一致。把它们判成冲突会让复核列表塞满
    噪声，真正需要人看的冲突反而被淹没。
    """
    if field in ("ARTISTSORT", "ALBUMARTISTSORT"):
        # 排序名的**顺序没有意义**，分隔符更是写法差异。
        # 成员集合相等，或去掉全部分隔符后的字符串相等，都算一致：
        # 前者处理「顺序不同」，后者处理「A with B, C」这类连写。
        return (
            _artist_tokens(field, now) == _artist_tokens(field, want)
            or _sort_key(now) == _sort_key(want)
        )
    if field.startswith("MUSICBRAINZ_") and field.endswith("ID"):
        def ids(values: Sequence[str]) -> list[str]:
            out: list[str] = []
            for value in values:
                out.extend(part for part in re.split(r"[\s;]+", str(value)) if part)
            return sorted(part.casefold() for part in out)
        return ids(now) == ids(want)
    if field in _DATE_FIELDS:
        # 任一组里的日期只要精确相等或互为前缀，就算一致。
        # 曲库有些文件同时写了两个发行日期；来源命中其中一个就不必改。
        for a in now:
            for b in want:
                left, right = a.strip(), b.strip()
                if left and right and (
                    left == right
                    or left.startswith(right + "-")
                    or right.startswith(left + "-")
                ):
                    return True
                # 完整日期只差一天：时区或地区发行日不同，是同一发行。
                da, db = _parse_date(left), _parse_date(right)
                if da is not None and db is not None and abs((da - db).days) <= 1:
                    return True
    if field in _ORDER_FREE or field in _NUMERIC_FIELDS:
        return sorted(_norm_values(field, now)) == sorted(_norm_values(field, want))
    # 单值字段（标题/专辑/日期等）同样走折叠比较：简繁、全半角、
    # 冠词倒置、人名空格差异都不算冲突。
    return _norm_values(field, now) == _norm_values(field, want)


def _album_edition_matches(
    now: Sequence[str], want: Sequence[str]
) -> bool:
    """本地专辑名是来源基础名的**更具体写法**，不算冲突。

    三种形态（都只认「本地更具体」这一个方向，反方向可能是另一个
    版次，仍进人工）：

    - 尾部版本括号：`Red (Taylor's Version)` / `Midnights [Jade Green
      Edition]`——剥掉后与来源一致；
    - 尾部双语注释/并排双名：`最偉大的作品 (Greatest Works of Art)`、
      `啓示錄 REVELATION`——基础名一致或来源是本地的尾部后缀；
    - 裸碟后缀：`Red CD1`——分碟文件的碟号写进了专辑名。
    """
    joined_now = " ".join(str(v) for v in now if str(v).strip())
    joined_want = " ".join(str(v) for v in want if str(v).strip())
    if not joined_now or not joined_want or joined_now == joined_want:
        return False
    base = _strip_edition(joined_now)
    if base != joined_now and _fold_value("ALBUM", base) == _fold_value(
        "ALBUM", joined_want
    ):
        return True
    # 尾部任意括号（双语注释等）：`最偉大的作品 (Greatest Works of Art)`
    # 剥掉后与来源一致——括号内容是注释，本地更全。
    bare = _EDITION_SUFFIX.sub("", joined_now).strip()
    if bare != joined_now and _fold_value("ALBUM", bare) == _fold_value(
        "ALBUM", joined_want
    ):
        return True
    # 双语名：来源（基础名）是本地名的尾部后缀，且来源本身够长
    # （≥5 个字符，避免「Love」撞上「Love Me Do」这类真不同）。
    fold_now = _fold_value("ALBUM", joined_now).replace(" ", "")
    fold_want = _fold_value("ALBUM", joined_want).replace(" ", "")
    return (
        len(fold_want) >= 5
        and fold_now.endswith(fold_want)
        and fold_now != fold_want
    )


def _is_placeholder_value(values: Sequence[str]) -> bool:
    """本地值是不是「没填」的占位（[unknown] 之类）。"""
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if _fold_value("ALBUM", text).replace(" ", "") in _PLACEHOLDER_VALUES:
            return True
    return False


def _date_lineage(
    now: Sequence[str],
    want: Sequence[str],
    suggested: Mapping[str, Sequence[str]],
) -> bool:
    """本地 DATE 与来源的**原始发行日**（ORIGINALDATE/ORIGINALYEAR）同谱系。

    实际案例：本地 `2012`，来源 DATE `2021-09-02`、ORIGINALDATE
    `2012-05-07`——本地记的是专辑最初发行年份，来源记的是这个版次的
    发行日，两者不矛盾。把这条判成冲突会让全库的再版专辑都进人工。
    """
    originals = [
        str(v).strip()
        for v in (suggested.get("ORIGINALDATE") or []) + (suggested.get("ORIGINALYEAR") or [])
        if str(v).strip()
    ]
    if not originals:
        return False
    for local in now:
        local = str(local).strip()
        if len(local) != 4 or not local.isdigit():
            continue  # 只处理「本地是纯年份」的形态
        for origin in originals:
            if origin.startswith(local):
                return True
    return False


#: 人名类字段集合的公开别名（供消解层引用，语义见上）。
_human_name_fields = _HUMAN_NAME_FIELDS


def compare_fields(
    current: Mapping[str, Sequence[str]],
    suggested: Mapping[str, Sequence[str]],
) -> list[dict]:
    """逐字段对比，给出建议动作。

    三种动作，对应三种完全不同的决策：

    - `same`：两边一致（含 `01`/`1`、大小写、多值顺序这类等价），无需动作。
    - `fill`：当前为空、来源有值 → **可以放心自动填充**，不覆盖任何信息。
    - `conflict`：两边都有值且不等价 → **必须人工确认**，自动流程绝不覆盖。

    排序把 `conflict` 放最前：复核者最该先看有风险的那几条。
    """
    rows: list[dict] = []
    for key in sorted(set(current) | set(suggested)):
        now = [str(v) for v in (current.get(key) or []) if str(v).strip()]
        want = [str(v) for v in (suggested.get(key) or []) if str(v).strip()]
        if not want:
            # 来源没有这个字段的值：不能建议「删掉当前的」——
            # 来源缺数据不等于当前的数据是错的。
            continue
        if key == "GENRE":
            # 流派是**补充信息**，不是身份信息。本地已经有流派时完全忽略
            # 来源的流派（默认「存在就可以忽略」）——否则「Pop」对
            # 来源的「Pop / Blue-eyed Soul / Pop Soul」会被判成冲突，
            # 几乎每首歌都进人工复核，把真正的冲突淹没掉。
            if now:
                action = ACTION_SAME
            else:
                want = want[:1]
                action = ACTION_FILL
            rows.append({
                "key": key,
                "current": now,
                "suggested": want,
                "action": action,
            })
            continue
        if not now:
            # 别名已覆盖：本地没有 TOTALTRACKS 但有相等的 TRACKTOTAL 时，
            # 不该再当成「缺一个字段」去写。
            if any(
                _same_value(key, current.get(alias) or [], want)
                for alias in _VALUE_ALIASES.get(key, ())
            ):
                action = ACTION_SAME
            else:
                action = ACTION_FILL
        elif key in ("ALBUM", "ARTIST", "ALBUMARTIST") and _is_placeholder_value(now):
            # 本地是占位值（[unknown] 之类）＝「没填」，不是人工痕迹：
            # 来源值直接按补齐处理。
            action = ACTION_FILL
        elif key == "ALBUM" and _album_edition_matches(now, want):
            # 本地是同一张专辑的**具体版次**（尾部版本括号），来源是基础名：
            # 本地更具体、且是人工整理过的痕迹，保留本地，不算冲突。
            action = ACTION_SAME
        elif key == "DATE" and _date_lineage(now, want, suggested):
            # 本地年份与来源的**原始发行日**同谱系：来源日期是该版次的发行日，
            # 本地记的是原始发行——两者都对，不算冲突。
            action = ACTION_SAME
        elif _same_value(key, now, want) or any(
            current.get(alias)
            and _same_value(key, current.get(alias) or [], want)
            for alias in _VALUE_ALIASES.get(key, ())
        ):
            action = ACTION_SAME
        elif _is_extension(key, now, want):
            # 只缺合作艺人/成员：补上不丢已有信息，按「可补」处理。
            action = ACTION_FILL
        else:
            action = ACTION_CONFLICT
        rows.append({
            "key": key,
            "current": now,
            "suggested": want,
            "action": action,
        })
    rows.sort(key=lambda r: (_ACTION_ORDER[r["action"]], r["key"]))
    return rows

def summarise(rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """给界面用的计数：冲突几条、可填充几条、一致几条。"""
    counts = {
        ACTION_CONFLICT: 0, ACTION_CORRECT: 0, ACTION_FILL: 0, ACTION_SAME: 0,
    }
    for row in rows:
        action = str(row.get("action") or "")
        if action in counts:
            counts[action] += 1
    return {
        "conflict": counts[ACTION_CONFLICT],
        "correct": counts[ACTION_CORRECT],
        "fill": counts[ACTION_FILL],
        "same": counts[ACTION_SAME],
        "total": len(rows),
    }


_FOLD_STRIP = re.compile(r"[\W_]+", re.UNICODE)


def _fold_title(text: str) -> str:
    """比较标题用的规范化：去大小写、空格与标点，保留中日韩字母数字。

    `\\W` 在 Unicode 模式下**不会**吃掉中日韩字符（它们是字母），所以
    「我們的夢」和「我們的夢 (Live)」都能正常比较，而「我们的梦」与
    「我們的夢」仍然不同——简繁差异不在这里处理，那是 `zhconv` 的事。
    """
    return _FOLD_STRIP.sub("", (text or "").casefold())


def _title_ratio(a: str, b: str) -> float:
    left, right = _fold_title(a), _fold_title(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _number_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"^0+(?=\d)", "", text)
    return digits


def locate_release_track(
    release: Mapping[str, object],
    *,
    release_track_id: str = "",
    recording_id: str = "",
    title: str = "",
    duration: float | None = None,
    track_number: str = "",
    disc_number: str = "",
) -> tuple[dict | None, dict | None, str]:
    """在一张发行的曲目表里定位「这个文件对应哪一条」。

    返回 `(曲目, 介质, 判定方式)`。判定方式用于给置信度定级：

    - `release_track_id` / `recording_id`：身份锚点，没有歧义；
    - `title+duration` / `title` / `number+duration`：推断，必须有足够证据。

    猜错会把曲序和 release-track-id 写到同一张专辑的别的曲目上，
    比不填严重得多，所以**证据不足就返回空**，绝不拿第一条凑数。
    """
    media = [m for m in (release.get("media") or []) if isinstance(m, dict)]
    if not media:
        return None, None, "none"

    all_tracks: list[tuple[dict, dict]] = []
    for medium in media:
        for track in _medium_tracks(medium):
            all_tracks.append((track, medium))

    if release_track_id:
        for track, medium in all_tracks:
            if str(track.get("id") or "") == release_track_id:
                return track, medium, "release_track_id"
    if recording_id:
        for track, medium in all_tracks:
            recording = track.get("recording")
            if isinstance(recording, dict) and str(recording.get("id") or "") == recording_id:
                return track, medium, "recording_id"

    if not title.strip() and not duration:
        return None, None, "none"

    best: tuple[dict, dict] | None = None
    best_score = 0.0
    best_method = "none"
    for track, medium in all_tracks:
        recording = track.get("recording")
        recording = recording if isinstance(recording, dict) else {}
        track_title = str(track.get("title") or recording.get("title") or "")
        ratio = _title_ratio(title, track_title)
        score = 0.0
        method = ""
        if ratio >= 0.999:
            score += 2.2
            method = "title"
        elif ratio >= 0.82:
            score += 1.4 * ratio
            method = "title"
        length_ms = track.get("length")
        delta: float | None = None
        if duration and isinstance(length_ms, (int, float)) and length_ms:
            delta = abs(float(length_ms) / 1000.0 - float(duration))
            if delta <= 2.0:
                score += 1.5
                method = "title+duration" if method else "duration"
            elif delta <= 5.0:
                score += 1.0
                method = "title+duration" if method else "duration"
            elif delta <= 10.0:
                score += 0.3
        number_match = bool(track_number) and _number_key(track.get("number")) == _number_key(track_number)
        if number_match:
            score += 0.8
            if method in ("", "duration"):
                method = "number+duration" if delta is not None else "number"
        if (
            disc_number
            and medium.get("position") is not None
            and _number_key(medium.get("position")) == _number_key(disc_number)
        ):
            score += 0.2
        if score > best_score:
            best, best_score, best_method = (track, medium), score, method

    # 接受门槛：标题相同，或「标题很像 + 时长很近」，或「曲序相同 + 时长很近」。
    # 单个弱信号（比如只差 4 秒）不足以认定。
    if best is not None:
        track, _medium = best
        recording = track.get("recording")
        recording = recording if isinstance(recording, dict) else {}
        ratio = _title_ratio(
            title, str(track.get("title") or recording.get("title") or "")
        )
        if ratio >= 0.999 or (ratio >= 0.82 and best_score >= 2.0) or best_score >= 2.3:
            return best[0], best[1], best_method or "mixed"
    return None, None, "none"


def _pick_track(
    release: Mapping[str, object],
    *,
    release_track_id: str = "",
    recording_id: str = "",
    title: str = "",
    duration: float | None = None,
    track_number: str = "",
    disc_number: str = "",
) -> tuple[dict | None, dict | None]:
    track, medium, _method = locate_release_track(
        release,
        release_track_id=release_track_id,
        recording_id=recording_id,
        title=title,
        duration=duration,
        track_number=track_number,
        disc_number=disc_number,
    )
    return track, medium


def build_release_candidate(
    provider: MusicBrainzProvider,
    release: Mapping[str, object],
    release_id: str,
    track: Mapping[str, object],
    medium: Mapping[str, object] | None,
    *,
    include_credits: bool = True,
    include_genres: bool = True,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """定位到曲目后，把发行级 + 曲目级字段合成一份可写入的候选。"""
    fields = _release_fields(provider, release, release_id)
    fields.update(_track_fields(track, medium))

    recording = track.get("recording")
    recording_id = str(
        (recording if isinstance(recording, dict) else {}).get("id") or ""
    )
    if recording_id and (include_credits or not fields.get("ISRC")):
        detail = provider.recording_detail(recording_id)
        if detail is not None:
            fields.update(_isrc_fields(detail))
            if include_credits:
                fields.update(provider.credits_for_recording(recording_id))

    if include_genres:
        genres = provider.genres_for_release(release)
        if genres:
            fields["GENRE"] = list(genres)

    extra: dict[str, str] = {
        "来源": "按 MBID 精确查询",
        "发行": str(release.get("title") or ""),
        "发行状态": str(release.get("status") or ""),
        "发行地区": str(release.get("country") or ""),
    }
    if release.get("disambiguation"):
        extra["消歧义"] = str(release["disambiguation"])
    if medium is not None and medium.get("format"):
        extra["介质"] = str(medium["format"])
    return fields, extra


def _unique(values: Sequence[object]) -> list[str]:
    """保序去重。标签是多值字段，重复值只会让播放器显示两遍。"""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _release_fields(
    provider: MusicBrainzProvider,
    release: Mapping[str, object],
    release_id: str,
) -> dict[str, list[str]]:
    """把一张发行的完整 JSON 映射成可写入的标签字段。

    键名遵循 Picard 的写法（`RELEASECOUNTRY` / `RELEASESTATUS` /
    `MUSICBRAINZ_*`），因为曲库就是 Picard 整理的——用别的写法会
    在同一首歌上制造两套含义相同的标签。
    """
    fields: dict[str, list[str]] = {
        "ALBUM": [str(release.get("title") or "").strip()],
        "MUSICBRAINZ_ALBUMID": [release_id.strip()],
    }
    date = str(release.get("date") or "").strip()
    if date:
        fields["DATE"] = [date]
    country = str(release.get("country") or "").strip()
    if country:
        fields["RELEASECOUNTRY"] = [country]
    status = str(release.get("status") or "").strip()
    if status:
        fields["RELEASESTATUS"] = [status.casefold()]
    barcode = str(release.get("barcode") or "").strip()
    if barcode:
        fields["BARCODE"] = [barcode]
    asin = str(release.get("asin") or "").strip()
    if asin:
        fields["ASIN"] = [asin]
    str(release.get("disambiguation") or "").strip()

    labels: list[str] = []
    catalogs: list[str] = []
    for info in release.get("label-info") or []:
        if not isinstance(info, dict):
            continue
        label = info.get("label") if isinstance(info.get("label"), dict) else {}
        name = str((label or {}).get("name") or "").strip()
        if name:
            labels.append(name)
        catalog = str(info.get("catalog-number") or "").strip()
        if catalog:
            catalogs.append(catalog)
    if labels:
        fields["LABEL"] = _unique(labels)
    if catalogs:
        fields["CATALOGNUMBER"] = _unique(catalogs)

    text = release.get("text-representation")
    if isinstance(text, dict):
        script = str(text.get("script") or "").strip()
        if script:
            fields["SCRIPT"] = [script]

    rg = release.get("release-group")
    rg = rg if isinstance(rg, dict) else {}
    if rg.get("id"):
        fields["MUSICBRAINZ_RELEASEGROUPID"] = [str(rg["id"])]
    types: list[str] = []
    if rg.get("primary-type"):
        types.append(str(rg["primary-type"]).casefold())
    for second in rg.get("secondary-types") or []:
        if str(second).strip():
            types.append(str(second).casefold())
    if types:
        fields["RELEASETYPE"] = _unique(types)
    original = str(rg.get("first-release-date") or "").strip()
    if original:
        fields["ORIGINALDATE"] = [original]
        if re.match(r"^\d{4}", original):
            fields["ORIGINALYEAR"] = [original[:4]]

    # 真实响应里是 `artist-credit`（单数），旧测试夹具写的是复数；
    # 两个都认，否则从实际运行拉到的发行永远补不上专辑艺术家。
    credits = release.get("artist-credit") or release.get("artist-credits") or []
    artists, joined, sorts, artist_ids = _artist_credit_details(credits)
    if artists:
        fields["ALBUMARTIST"] = [joined or "/".join(artists)]
        fields["ALBUMARTISTS"] = artists
        if sorts and len(sorts) == len(artists):
            fields["ALBUMARTISTSORT"] = ["; ".join(sorts)]
        if artist_ids:
            fields["MUSICBRAINZ_ALBUMARTISTID"] = artist_ids

    media = [m for m in (release.get("media") or []) if isinstance(m, dict)]
    if media:
        fields["TOTALDISCS"] = [str(len(media))]
        fields["DISCTOTAL"] = [str(len(media))]

    return fields


def _track_fields(
    track: Mapping[str, object],
    medium: Mapping[str, object] | None,
) -> dict[str, list[str]]:
    """把一张发行的某条曲目映射成标签字段。"""
    fields: dict[str, list[str]] = {}
    number = str(track.get("number") or "").strip()
    # 黑胶发行用 A1/B5 这种面+序号；本地数字文件用 11。两种编号都对，
    # 但把 B5 写进数字文件会破坏播放顺序，所以只在来源是纯数字时建议。
    if number and re.fullmatch(r"\d+", number):
        fields["TRACKNUMBER"] = [number]
    if medium is not None and medium.get("position"):
        fields["DISCNUMBER"] = [str(medium["position"])]
    track_title = str(track.get("title") or "").strip()
    if track_title:
        fields["TITLE"] = [track_title]
    recording = track.get("recording")
    recording = recording if isinstance(recording, dict) else {}
    if recording.get("id"):
        fields["MUSICBRAINZ_TRACKID"] = [str(recording["id"])]
    isrcs = [
        str(item.get("id") if isinstance(item, dict) else item)
        for item in (recording.get("isrcs") or [])
    ]
    isrcs = [code.strip() for code in isrcs if code and code.strip()]
    if isrcs:
        fields["ISRC"] = _unique(isrcs)
    if track.get("id"):
        fields["MUSICBRAINZ_RELEASETRACKID"] = [str(track["id"])]
    if medium is not None:
        count = medium.get("track-count")
        tracks = _medium_tracks(medium)
        if isinstance(count, int) and count > 0:
            fields["TOTALTRACKS"] = [str(count)]
            fields["TRACKTOTAL"] = [str(count)]
        elif tracks:
            fields["TOTALTRACKS"] = [str(len(tracks))]
            fields["TRACKTOTAL"] = [str(len(tracks))]
    names, joined, sorts, artist_ids = _artist_credit_details(
        recording.get("artist-credit") or []
    )
    if names:
        fields["ARTIST"] = [joined or "/".join(names)]
        fields["ARTISTS"] = names
        if sorts and len(sorts) == len(names):
            fields["ARTISTSORT"] = ["; ".join(sorts)]
        if artist_ids:
            fields["MUSICBRAINZ_ARTISTID"] = artist_ids
    return fields


def lookup_by_release(
    provider: MusicBrainzProvider,
    *,
    release_id: str,
    recording_id: str = "",
    release_track_id: str = "",
    title: str = "",
    duration: float | None = None,
    track_number: str = "",
    disc_number: str = "",
    include_credits: bool = True,
    include_genres: bool = True,
) -> MetadataCandidate | None:
    """**按文件里已有的 MBID 精确查询**，而不是重新搜索。

    ## 为什么这是首选路径

    曲库里 97% 的文件已被 Picard 写入 MBID。重新搜索的返回是
    「这首录音出现在哪些发行上」里挑出来的若干条，**往往不是文件原本所属的
    那张发行**——实际同一首歌搜出来的两张专辑都不是本地那张，
    于是 ALBUM / DATE / TITLE 全被误报成「冲突」，全是假冲突。

    按 `MUSICBRAINZ_ALBUMID` 直接拉那张发行，再按录音 ID 定位到具体曲目，
    结果**唯一且精确**，不需要人工在候选里挑。

    搜索只在文件缺 MBID 时兜底使用。
    """
    if not provider.available or not release_id.strip():
        return None

    release = provider.release_detail(release_id)
    if release is None:
        return None

    # **不要叫 title**：参数 `title` 是「本地文件的标题」，用同一个名字
    # 覆盖它会让下面的曲目定位拿发行的标题去比对，静默匹配不到任何曲目。
    release_title = str(release.get("title") or "").strip()
    if not release_title:
        return None

    chosen_track, chosen_medium, method = locate_release_track(
        release,
        release_track_id=release_track_id,
        recording_id=recording_id,
        title=title,
        duration=duration,
        track_number=track_number,
        disc_number=disc_number,
    )
    if chosen_track is not None:
        fields, extra = build_release_candidate(
            provider,
            release,
            release_id,
            chosen_track,
            chosen_medium,
            include_credits=include_credits,
            include_genres=include_genres,
        )
    else:
        # 定位不到曲目：发行级字段仍然给出（专辑、日期、厂牌、发行组），
        # 但不写曲序——猜错曲序比不填严重得多。
        fields = _release_fields(provider, release, release_id)
        extra = {
            "来源": "按 MBID 精确查询",
            "发行": release_title,
            "发行状态": str(release.get("status") or ""),
            "发行地区": str(release.get("country") or ""),
        }
        if include_genres:
            genres = provider.genres_for_release(release)
            if genres:
                fields["GENRE"] = list(genres)

    target_recording = recording_id.strip() or str(
        (fields.get("MUSICBRAINZ_TRACKID") or [""])[0]
    )
    if target_recording and chosen_track is None:
        # 曲目没定位到时，录音级字段仍然属于这个文件（ISRC、艺人）。
        detail = provider.recording_detail(target_recording)
        if detail is not None:
            fields.update(_isrc_fields(detail))
            names, joined, _sorts, _ids = _artist_credit_details(
                detail.get("artist-credit") or []
            )
            if names:
                fields.setdefault("ARTIST", [joined or "/".join(names)])
                fields.setdefault("ARTISTS", names)
            if include_credits:
                fields.update(provider.credits_for_recording(target_recording))

    length_ms = (chosen_track or {}).get("length") or release.get("length")
    found_duration = (
        float(length_ms) / 1000.0
        if isinstance(length_ms, (int, float)) and length_ms
        else None
    )
    extra["定位"] = {
        "release_track_id": "发行曲目 MBID",
        "recording_id": "录音 MBID",
    }.get(method, "标题与时长" if method != "none" else "未定位")

    return MetadataCandidate(
        provider_id=str((chosen_track or {}).get("id") or ""),
        title=str((fields.get("TITLE") or [release_title])[0]),
        artists=tuple(fields.get("ARTISTS") or ()),
        album=release_title,
        duration=found_duration,
        score=100,
        fields=fields,
        extra=extra,
    )

# ---------------------------------------------------------------------------
# 链接直查：从一条 MusicBrainz 链接取回可写入的字段
# ---------------------------------------------------------------------------
#
# 为什么需要这条路：
#
# 1. 搜索靠「标题 + 艺人」，而本地标签**恰好可能是坏的**（整个署名串塞进
#    TITLE、繁体简体混用、上传者冒名记录）。这时搜索给出的候选全都不对，
#    人工也没法在几十条里挑。
# 2. 使用者手上往往已经有一条确定的链接（MusicBrainz 网页 / Picard 里点出来的）。
#    那是**人工断言「就是这一条」**，比任何相似度都可靠。
# 3. 缺 MUSICBRAINZ_* 的文件，靠人手工抄 UUID 既痛苦又容易错——这正是
#    「缺 MusicBrainz ID」这个缺陷一直修不掉的真正原因。
#
# 所以：链接是被信任的输入，但仍然只做「逐字段对比 + 人工勾选」，
# 绝不整表覆盖。同一首录音出现在多张发行上时**全部列出**，按与本地
# 专辑的接近程度排序，由人确认——实际同一首录音同时挂在单曲和合辑上，
# 选错那张会把 ALBUMID / RELEASEGROUPID 写成另一张发行。

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

#: 支持的实体类型 → 中文说明（界面直接展示，别让使用者猜哪个能用）
LINK_ENTITY_LABELS = {
    "recording": "录音",
    "release": "发行",
    "release-group": "发行组",
    "artist": "艺人",
}


class MbidLinkError(ValueError):
    """链接不是可识别的 MusicBrainz 实体链接。

    单独一个异常类型，是为了让接口层能把它和「上游请求失败」分开：
    前者是用户输入问题（400），后者是来源问题（502），处理方式完全不同。
    """


@dataclass(frozen=True, slots=True)
class EntityLink:
    """一条已解析的 MusicBrainz 实体链接。"""

    entity: str
    """recording / release / release-group / artist"""

    mbid: str
    """规范化后的 UUID（小写）。"""

    url: str
    """规范化的公开网页地址，便于回显给使用者核对。"""

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "entity_label": LINK_ENTITY_LABELS.get(self.entity, self.entity),
            "mbid": self.mbid,
            "url": self.url,
        }


def _host_is_musicbrainz(host: str) -> bool:
    """只认 musicbrainz.org 及其子域。

    必须按**域名边界**判断，不能 `"musicbrainz.org" in host`——
    那样 `musicbrainz.org.evil.com` 也会通过。链接里的 ID 不涉及凭据，
    但把「来源」判错就等于把数据判错。
    """
    host = (host or "").casefold().rstrip(".")
    return host == "musicbrainz.org" or host.endswith(".musicbrainz.org")


def parse_entity_link(text: str) -> EntityLink:
    """把一条 MusicBrainz 链接解析成 (实体, MBID)。

    接受的形式（都能从 MusicBrainz 网页或 Picard 里直接复制出来）：

        https://musicbrainz.org/recording/449ff670-1277-4245-8cd9-7dfe42698b54
        musicbrainz.org/release/e063e624-451a-4901-8eb6-84e219a03d80/
        https://beta.musicbrainz.org/release-group/...
        https://musicbrainz.org/ws/2/artist/...?fmt=json

    解析失败抛 `MbidLinkError`（带可直接展示的中文说明）。
    """
    raw = (text or "").strip()
    if not raw:
        raise MbidLinkError("没有填入链接")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parts = urlsplit(raw)
    if not _host_is_musicbrainz(parts.hostname or ""):
        raise MbidLinkError(
            "这不是 MusicBrainz 的链接（域名应为 musicbrainz.org）："
            f"{parts.hostname or raw}"
        )

    segments = [s for s in parts.path.split("/") if s]
    # /ws/2/<实体>/<id> 与网页地址 /<实体>/<id> 都归一到同一形状
    if segments and segments[0] == "ws":
        segments = segments[1:]
    if segments and segments[0].isdigit():
        segments = segments[1:]
    if len(segments) < 2:
        raise MbidLinkError(
            "链接里没有 ID。请复制具体条目页的地址，例如 "
            "https://musicbrainz.org/recording/<id>"
        )

    entity = segments[0].casefold()
    if entity not in LINK_ENTITY_LABELS:
        raise MbidLinkError(
            f"不支持的条目类型「{segments[0]}」。目前支持：" +
            "、".join(f"{k}（{v}）" for k, v in LINK_ENTITY_LABELS.items())
        )
    mbid = segments[1].strip().casefold()
    if not _UUID_RE.match(mbid):
        raise MbidLinkError(f"「{segments[1]}」不是有效的 MusicBrainz ID（UUID 格式）")
    return EntityLink(entity=entity, mbid=mbid, url=f"https://musicbrainz.org/{entity}/{mbid}")


def _local_alignment(
    candidate: MetadataCandidate, *, album: str, album_id: str
) -> tuple[int, bool, bool]:
    """判断一条候选与本地文件「像不像同一张发行」。

    两个信号**互相独立**，且都可能出错：
      · 本地 ALBUM 名——人看得见、但可能被翻译或改过；
      · 本地 MUSICBRAINZ_ALBUMID——机器写的、但可能来自一次错的旧匹配。
    实际同一首录音同时挂在「单曲」和「合辑」上，本地 ALBUM 名指向合辑、
    ALBUMID 却指向单曲。这种时候不能自作主张：按专辑名优先排序，
    再把两个信号的判定**显示出来**让人定。
    """
    got_album_id = str((candidate.fields.get("MUSICBRAINZ_ALBUMID") or [""])[0]).casefold()
    got_album = str((candidate.fields.get("ALBUM") or [""])[0])
    id_match = bool(album_id.strip()) and got_album_id == album_id.strip().casefold()
    name_match = bool(album.strip()) and _fold_title(got_album) == _fold_title(album)
    score = (2 if name_match else 0) + (1 if id_match else 0)
    return score, name_match, id_match


def lookup_by_link(
    provider: MusicBrainzProvider,
    link: EntityLink,
    *,
    title: str = "",
    duration: float | None = None,
    album: str = "",
    album_id: str = "",
    recording_id: str = "",
    release_track_id: str = "",
    max_releases: int = 5,
) -> list[MetadataCandidate]:
    """按链接取回候选（每张发行一条），并按与本地专辑的接近程度排序。

    返回空列表表示「链接有效，但来源没有可用字段」——与抛异常区分开。
    """
    if not provider.available:
        return []

    if link.entity == "recording":
        candidates = _candidates_from_recording(
            provider,
            link.mbid,
            title=title,
            duration=duration,
            release_track_id=release_track_id,
            max_releases=max_releases,
        )
    elif link.entity == "release":
        one = lookup_by_release(
            provider,
            release_id=link.mbid,
            recording_id=recording_id,
            release_track_id=release_track_id,
            title=title,
            duration=duration,
        )
        candidates = [one] if one is not None else []
    elif link.entity == "release-group":
        candidates = _candidates_from_release_group(
            provider,
            link.mbid,
            title=title,
            duration=duration,
            album=album,
            album_id=album_id,
            recording_id=recording_id,
            release_track_id=release_track_id,
            max_releases=max_releases,
        )
    else:  # artist
        candidates = [_candidate_from_artist(provider, link.mbid)]

    ranked: list[tuple[int, int, MetadataCandidate]] = []
    for order, cand in enumerate(c for c in candidates if c is not None):
        score, name_match, id_match = _local_alignment(cand, album=album, album_id=album_id)
        extra = dict(cand.extra)
        if not album.strip() and not album_id.strip():
            extra.pop("本地对照", None)
        elif name_match and id_match:
            extra["本地对照"] = "专辑名与 MBID 都一致"
        elif name_match:
            extra["本地对照"] = "专辑名一致，MBID 不同"
        elif id_match:
            extra["本地对照"] = "MBID 一致，专辑名不同"
        else:
            extra["本地对照"] = "与本地专辑名和 MBID 都不同"
        ranked.append((score, order, MetadataCandidate(
            provider=cand.provider,
            provider_id=cand.provider_id,
            title=cand.title,
            artists=cand.artists,
            album=cand.album,
            duration=cand.duration,
            score=cand.score,
            fields=cand.fields,
            extra=extra,
        )))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [cand for _score, _order, cand in ranked]


def _candidate_from_artist(
    provider: MusicBrainzProvider, mbid: str
) -> MetadataCandidate | None:
    """艺人链接：只能给出艺人名与艺人 ID。"""
    try:
        detail = provider._get(  # noqa: SLF001 — 同模块内协作
            f"/ws/2/artist/{mbid}", {"fmt": "json"}
        )
    except Exception:  # noqa: BLE001 — 查不到就当作没有候选
        return None
    name = str(detail.get("name") or "").strip()
    fields: dict[str, list[str]] = {"MUSICBRAINZ_ARTISTID": [mbid]}
    if name:
        fields["ARTIST"] = [name]
        fields["ARTISTS"] = [name]
    return MetadataCandidate(
        provider_id=mbid, title=name, artists=(name,) if name else (),
        score=100, fields=fields,
        extra={"来源": "链接直查（艺人）"},
    )


def _candidates_from_recording(
    provider: MusicBrainzProvider,
    mbid: str,
    *,
    title: str,
    duration: float | None,
    release_track_id: str,
    max_releases: int,
) -> list[MetadataCandidate]:
    """录音链接 → 它出现的每张发行各一条候选。

    **为什么不是只取一条。** 同一首录音同时挂在单曲、原专辑、精选合辑上
    是常态（实际曲库某首歌既有单曲又有 2026 年合辑）。选错那张，就会把
    ALBUMID / RELEASEGROUPID 写成另一张发行，比不填更糟。

    录音级字段（TRACKID / ARTIST / ISRC）对所有发行都成立，先取一次再合并。
    """
    try:
        detail = provider._get(  # noqa: SLF001 — 同模块内协作
            f"/ws/2/recording/{mbid}",
            {"fmt": "json",
             "inc": "isrcs+artist-credits+releases+release-groups"},
        )
    except Exception:  # noqa: BLE001 — 取不到就当作没有候选
        return []

    names, joined = _first_artist_credit(detail.get("artist-credit") or [])
    base: dict[str, list[str]] = {"MUSICBRAINZ_TRACKID": [mbid]}
    if names:
        base["ARTIST"] = [joined or "/".join(names)]
        base["ARTISTS"] = names
    base.update(_isrc_fields(detail))
    artist_ids = [
        str((a.get("artist") or {}).get("id"))
        for a in (detail.get("artist-credit") or [])
        if isinstance(a, dict) and isinstance(a.get("artist"), dict)
        and (a.get("artist") or {}).get("id")
    ]
    if artist_ids:
        base["MUSICBRAINZ_ARTISTID"] = artist_ids
    recording_title = str(detail.get("title") or "").strip()

    out: list[MetadataCandidate] = []
    releases = [r for r in (detail.get("releases") or [])
                if isinstance(r, dict) and r.get("id")]
    for release in releases[:max_releases]:
        release_id = str(release["id"])
        cand = lookup_by_release(
            provider,
            release_id=release_id,
            recording_id=mbid,
            release_track_id=release_track_id,
            title=title or recording_title,
            duration=duration,
        )
        if cand is None:
            continue
        fields = {**base, **{k: list(v) for k, v in cand.fields.items()}}
        extra = dict(cand.extra)
        # 发行本身的信息（年份/状态）用它自己的，别被别的发行盖掉
        label = str(release.get("title") or "").strip()
        if label:
            extra["发行"] = label
        out.append(MetadataCandidate(
            provider=cand.provider,
            provider_id=mbid,
            title=cand.title or recording_title,
            artists=cand.artists or tuple(names),
            album=str((fields.get("ALBUM") or [""])[0]),
            duration=cand.duration,
            score=100,
            fields=fields,
            extra=extra,
        ))

    if out:
        return out
    # 一张发行都没有（极少见）：仍然给出录音级字段，总比什么都没有强
    return [MetadataCandidate(
        provider_id=mbid,
        title=recording_title,
        artists=tuple(names),
        duration=detail.get("length") / 1000.0
        if isinstance(detail.get("length"), (int, float)) else None,
        score=100,
        fields=base,
        extra={"来源": "链接直查（录音，无发行信息）"},
    )]


def _candidates_from_release_group(
    provider: MusicBrainzProvider,
    mbid: str,
    *,
    title: str,
    duration: float | None,
    album: str,
    album_id: str,
    recording_id: str,
    release_track_id: str,
    max_releases: int,
) -> list[MetadataCandidate]:
    """发行组链接 → 组内每张发行各一条候选（按与本地专辑的接近程度取前几张）。"""
    try:
        detail = provider._get(  # noqa: SLF001 — 同模块内协作
            f"/ws/2/release-group/{mbid}",
            {"fmt": "json", "inc": "releases+artist-credits"},
        )
    except Exception:  # noqa: BLE001 — 取不到就当作没有候选
        return []

    releases = [r for r in (detail.get("releases") or [])
                if isinstance(r, dict) and r.get("id")]
    # 先按「是不是本地那张」预排，避免发行组很大时把真正那张截掉
    releases.sort(key=lambda r: (
        0 if album_id.strip() and str(r.get("id", "")).casefold() == album_id.strip().casefold() else 1,
        0 if album.strip() and _fold_title(str(r.get("title") or "")) == _fold_title(album) else 1,
        str(r.get("date") or "9999"),
    ))

    out: list[MetadataCandidate] = []
    for release in releases[:max_releases]:
        cand = lookup_by_release(
            provider,
            release_id=str(release["id"]),
            recording_id=recording_id,
            release_track_id=release_track_id,
            title=title,
            duration=duration,
        )
        if cand is not None:
            out.append(cand)
    return out
