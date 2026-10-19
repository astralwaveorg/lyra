"""全库统计：把审计结果整理成一眼能看懂的数目。

为什么单独成模块：同样的数字要同时出现在 `lyra stats` 的终端输出和 Web 总览页上，
两处各写一遍必然逐渐不一致。

统计口径上有两处容易混淆，这里明确区分：

1. **`.lrc` 文件总数 ≠ 有歌词的曲目数。** 曲库里有 178 个 0 字节占位 lrc；
   它们「是文件」，但不含任何歌词。所以既报文件数，也报其中真正有内容/带时间轴的数目。
2. **外置 lrc 与内嵌歌词是两条独立通道。** 默认两者都要（D1），
   因此分别统计，再给出「至少有一条通道有有效歌词」的覆盖率。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from .audit import audit_root
from .config import RootConfig
from .lang import priority_group
from .models import IssueCode, LyricsState

__all__ = ["LibraryStats", "collect_stats", "render_stats_text", "top_items"]

#: 图表里最多显示多少项，其余归入「其他」。
#: 为什么必须有上限：实际曲库有 71 张专辑、32 个歌手串。全画出来在手机屏上
#: 每一格都小到看不清标签，还不如把长尾合并成一项——它本来就只是背景噪声。
TOP_ALBUMS = 24
TOP_ARTISTS = 8
TOP_GENRES = 12
TOP_LABELS = 8


def top_items(counts: dict[str, int], limit: int) -> list[dict[str, object]]:
    """把计数字典整理成「降序 + 长尾合并」的列表。

    返回值是列表而不是字典：图表需要**稳定顺序**，而字典顺序在 JSON 往返后
    不保证（前端只能再排一次，两处排序口径迟早会不一致）。
    """
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    head = ordered[:limit]
    tail = ordered[limit:]
    out: list[dict[str, object]] = [
        {"name": name, "count": count} for name, count in head
    ]
    if tail:
        out.append({
            "name": f"其他 {len(tail)} 项",
            "count": sum(count for _name, count in tail),
            "is_other": True,
        })
    return out


#: GENRE 字段里常见的多值分隔符。实际 `Mandopop`、`Pop;Cantopop` 两种都有。
_GENRE_SPLIT = re.compile(r"[;,/、，；|]")
_YEAR = re.compile(r"(19|20)\d{2}")


def _first(tags: dict[str, list[str]], key: str) -> str:
    values = tags.get(key) or []
    return str(values[0]).strip() if values else ""


def _split_genres(tags: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for raw in tags.get("GENRE") or []:
        for piece in _GENRE_SPLIT.split(str(raw)):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


@dataclass(slots=True)
class LibraryStats:
    root_name: str = ""
    root_path: str = ""
    scanned_at: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0

    # -- 音频 --
    audio_total: int = 0
    by_format: dict[str, int] = field(default_factory=dict)
    unsupported_total: int = 0
    parse_failures: int = 0

    # -- 外置 .lrc --
    lrc_total: int = 0
    lrc_empty: int = 0
    """0 字节或纯空白：是文件，但没有歌词。"""
    lrc_synced: int = 0
    lrc_plain: int = 0
    lrc_suspicious: int = 0
    lrc_unparseable: int = 0
    lrc_bom: int = 0
    lrc_orphan: int = 0
    """没有对应音频的 lrc。"""
    lrc_duplicate_groups: int = 0
    """同目录下疑为同一曲目的多个 lrc。"""

    # -- 内嵌歌词 --
    embedded_synced: int = 0
    embedded_plain: int = 0
    embedded_absent: int = 0
    embedded_suspicious: int = 0
    without_embedded_cover: int = 0

    cover_total: int = 0
    """有内嵌封面的曲目数。"""
    cover_pct: float = 0.0

    # -- 覆盖率 --
    tracks_with_lyrics: int = 0
    """内嵌或 sidecar 至少有一侧是带时间轴的歌词。"""
    tracks_without_lyrics: int = 0
    coverage_pct: float = 0.0
    """「任一形式有歌词」的占比。

    **不要拿它当「歌词覆盖」展示给使用者。** 实际库里 161 首只有内嵌、
    没有 .lrc，它照样报 100%——而播放器、车机、网易云本地歌词只认 .lrc。
    界面上的「.lrc 覆盖」用的是 `sidecar_coverage_pct`。
    """

    # -- 按「.lrc 有没有」重新分桶（概览页的环形图用）--
    sidecar_timed: int = 0
    """有同名 .lrc 且带时间轴。"""
    sidecar_untimed: int = 0
    """有 .lrc 但内容无时间轴（纯文本/空占位）。"""
    sidecar_missing_embedded: int = 0
    """没有 .lrc，但内嵌有带时间轴歌词——补一下就有的那批。"""
    sidecar_missing_none: int = 0
    """没有 .lrc，内嵌也没有可用的带时间轴歌词。"""
    sidecar_coverage_pct: float = 0.0
    """带时间轴 .lrc 的覆盖率。**这才是使用者眼里的「歌词覆盖」**。"""
    embedded_coverage_pct: float = 0.0

    # -- 健康 --
    issues: dict[str, int] = field(default_factory=dict)
    issues_skipped_unfixable: int = 0
    """因格式不支持改写标签而未计入问题列表的缺陷条数（见 AuditSummary）。"""

    # -- 规模（由标签汇总，用于概览页的图表）--
    total_seconds: float = 0.0
    """全部音频的时长之和。曲库「有多大」比文件数更直观。"""
    album_total: int = 0
    album_other: int = 0
    """因篇幅未画进树状图的专辑数（各自只有一两首）。"""
    artist_total: int = 0
    genre_total: int = 0

    # -- 歌词覆盖的四种组合（环形图用）--
    coverage_both: int = 0
    """内嵌与 .lrc 都有带时间轴的歌词。"""
    coverage_embedded_only: int = 0
    coverage_sidecar_only: int = 0
    coverage_none: int = 0

    # -- 标签分布（已按「降序 + 长尾合并」整理）--
    genres: list[dict[str, object]] = field(default_factory=list)
    albums: list[dict[str, object]] = field(default_factory=list)
    artists: list[dict[str, object]] = field(default_factory=list)
    labels: list[dict[str, object]] = field(default_factory=list)
    decades: list[dict[str, object]] = field(default_factory=list)
    languages: list[dict[str, object]] = field(default_factory=list)
    duration_histogram: list[dict[str, object]] = field(default_factory=list)
    tag_coverage: list[dict[str, object]] = field(default_factory=list)
    """各标签字段有值的曲目数与占比。**这张表本身就是结论**：
    某一项覆盖率低时，基于它的图表不值得看，先补标签。"""

    def to_dict(self) -> dict:
        return asdict(self)


def collect_stats(root: RootConfig) -> LibraryStats:
    """扫描一个受管根并汇总统计（只读，不修改任何文件）。"""
    result = audit_root(root)
    s = result.summary
    stats = LibraryStats(
        root_name=root.name,
        root_path=str(root.resolved_path),
        elapsed_seconds=result.elapsed_seconds,
    )

    stats.audio_total = s.tracks_total
    stats.by_format = dict(s.by_format)
    stats.unsupported_total = s.by_format.get("unsupported", 0)
    stats.parse_failures = s.parse_failures

    states = result.sidecar_states
    stats.lrc_total = s.sidecar_total
    stats.lrc_empty = len(result.empty_sidecars)
    stats.lrc_synced = states.get(LyricsState.SYNCED.value, 0)
    stats.lrc_plain = states.get(LyricsState.PLAIN_TEXT.value, 0)
    stats.lrc_suspicious = states.get(LyricsState.SYNCED_SUSPICIOUS.value, 0)
    stats.lrc_unparseable = states.get(LyricsState.UNPARSEABLE.value, 0)
    stats.lrc_bom = len(result.bom_sidecars)
    stats.lrc_orphan = len(result.orphan_sidecars)
    stats.lrc_duplicate_groups = len(result.duplicate_sidecars)

    genre_counts: dict[str, int] = {}
    album_counts: dict[str, int] = {}
    artist_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    decade_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    tag_present: dict[str, int] = {}
    # 时长分桶：1 分钟一档到 8 分钟，两端各一个开口桶。
    # 不用等宽自动分桶：音乐时长集中在 3~5 分钟，自动分桶会把所有曲子塞进
    # 同一格，图上只剩一根柱子，看起来像坏了。
    duration_buckets = [0] * 10

    def _bucket(seconds: float) -> int:
        if seconds < 60:
            return 0
        index = int(seconds // 60)
        return min(index, 9) if index < 9 else 9

    #: 概览页最先要回答的是「标签全不全」——基于缺失字段的图表没有意义。
    tracked_tag_fields = (
        "TITLE", "ARTIST", "ARTISTS", "ALBUM", "ALBUMARTIST", "GENRE",
        "DATE", "TRACKNUMBER", "DISCNUMBER", "LABEL", "ISRC", "COMPOSER",
        "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID",
    )

    for track in result.tracks:
        tags = track.tags or {}
        st = track.embedded.state
        if st is LyricsState.SYNCED:
            stats.embedded_synced += 1
        elif st is LyricsState.SYNCED_SUSPICIOUS:
            stats.embedded_suspicious += 1
        elif st is LyricsState.PLAIN_TEXT:
            stats.embedded_plain += 1
        else:
            stats.embedded_absent += 1
        if not track.embedded.has_cover:
            stats.without_embedded_cover += 1

        embedded_ok = st is LyricsState.SYNCED
        sidecar_ok = track.sidecar.state is LyricsState.SYNCED
        if embedded_ok and sidecar_ok:
            stats.coverage_both += 1
        elif embedded_ok:
            stats.coverage_embedded_only += 1
        elif sidecar_ok:
            stats.coverage_sidecar_only += 1
        else:
            stats.coverage_none += 1
        if embedded_ok or sidecar_ok:
            stats.tracks_with_lyrics += 1
        else:
            stats.tracks_without_lyrics += 1

        # 按 .lrc 的四分桶：给「.lrc 覆盖率」一个**没有重叠**的分解。
        # 注意不能拿上面的 coverage_* 反推：那四个桶里「只有内嵌」与
        # 「.lrc 是纯文本」会重叠，加起来不等于曲目数。
        if sidecar_ok:
            stats.sidecar_timed += 1
        elif track.sidecar.exists:
            stats.sidecar_untimed += 1
        elif embedded_ok:
            stats.sidecar_missing_embedded += 1
        else:
            stats.sidecar_missing_none += 1

        stats.total_seconds += track.audio.duration or 0.0
        duration_buckets[_bucket(track.audio.duration or 0.0)] += 1

        for key in tracked_tag_fields:
            if tags.get(key):
                tag_present[key] = tag_present.get(key, 0) + 1

        album = _first(tags, "ALBUM")
        if album:
            album_counts[album] = album_counts.get(album, 0) + 1
        # 歌手优先用多值的 ARTISTS（Picard 会为合作曲目逐项写入），回退 ARTIST。
        # 这里**不做拆分**：图上要显示的是「这张专辑/这个署名下的曲目数」，
        # 把 `G.E.M. 鄧紫棋 & 周興哲` 拆成两个人只会让统计对不上曲目数。
        artist = _first(tags, "ARTISTS") or _first(tags, "ARTIST")
        if artist:
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
        label = _first(tags, "LABEL")
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
        for genre in _split_genres(tags):
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
        year = _YEAR.search(_first(tags, "DATE"))
        if year:
            decade = year.group(0)[:3] + "0 年代"
            decade_counts[decade] = decade_counts.get(decade, 0) + 1
        # 语种与「来源优先级」用**同一个判据**，避免两处对同一首歌给出
        # 不同答案（那种不一致最难发现，因为两边看起来都合理）。
        group = priority_group(
            _first(tags, "TITLE"),
            tuple((tags.get("ARTISTS") or tags.get("ARTIST") or [])[:2]),
            cjk_uses_zh_priority=True,
        )
        language_counts[{"zh": "华语", "foreign": "外语"}.get(group, group)] = (
            language_counts.get(
                {"zh": "华语", "foreign": "外语"}.get(group, group), 0
            ) + 1
        )

    stats.album_total = len(album_counts)
    stats.artist_total = len(artist_counts)
    stats.genre_total = len(genre_counts)
    stats.genres = top_items(genre_counts, TOP_GENRES)
    # 专辑分布**不做长尾合并**：树状图按面积表示数量，而 71 张专辑里有 47 张
    # 各只有 1 首——合并成一个「其他」会得到最大的一块，把真正想看的头部专辑
    # 挤成细条，图就白画了。改为只显示前 N 张，并在说明里写清省略了多少。
    stats.albums = top_items(album_counts, TOP_ALBUMS)
    stats.albums = [a for a in stats.albums if not a.get("is_other")]
    stats.album_other = len(album_counts) - len(stats.albums)
    stats.artists = top_items(artist_counts, TOP_ARTISTS)
    stats.labels = top_items(label_counts, TOP_LABELS)
    stats.decades = sorted(
        ({"name": name, "count": count} for name, count in decade_counts.items()),
        key=lambda item: str(item["name"]),
    )
    stats.languages = top_items(language_counts, 6)
    stats.duration_histogram = [
        {
            "name": (
                "< 1 分" if index == 0
                else f"{index}–{index + 1} 分" if index < 9
                else "9 分以上"
            ),
            "count": count,
        }
        for index, count in enumerate(duration_buckets)
    ]
    stats.tag_coverage = [
        {
            "name": key,
            "count": tag_present.get(key, 0),
            "pct": round(tag_present.get(key, 0) * 100 / max(stats.audio_total, 1), 1),
        }
        for key in tracked_tag_fields
    ]

    if stats.audio_total:
        stats.coverage_pct = round(
            stats.tracks_with_lyrics * 100 / stats.audio_total, 1
        )
        stats.sidecar_coverage_pct = round(
            stats.sidecar_timed * 100 / stats.audio_total, 1
        )
        stats.embedded_coverage_pct = round(
            stats.embedded_synced * 100 / stats.audio_total, 1
        )
        stats.cover_total = max(0, stats.audio_total - stats.without_embedded_cover)
        stats.cover_pct = round(stats.cover_total * 100 / stats.audio_total, 1)

    stats.issues = dict(s.by_issue)
    stats.issues_skipped_unfixable = s.issues_skipped_unfixable
    return stats


# ---------------------------------------------------------------------------
# 终端输出
# ---------------------------------------------------------------------------

_FORMAT_LABEL = {
    "flac": "FLAC",
    "ogg_vorbis": "OGG Vorbis",
    "ogg_opus": "OGG Opus",
    "mp3": "MP3",
    "unsupported": "不支持/无法识别",
}

_ISSUE_LABEL = {
    IssueCode.EMPTY_SIDECAR.value: "0 字节 lrc（可安全填充）",
    IssueCode.ORPHAN_SIDECAR.value: "孤儿 lrc（无对应音频）",
    IssueCode.DUPLICATE_SIDECAR.value: "疑似重复 lrc",
    IssueCode.SIDECAR_NAME_MISMATCH.value: "异名配对的 lrc",
    IssueCode.SIDECAR_AMBIGUOUS.value: "归属歧义的 lrc",
    IssueCode.SIDECAR_BOM.value: "带 BOM 的 lrc",
    IssueCode.CONTAINER_MISMATCH.value: "扩展名与容器不符",
    IssueCode.UNSUPPORTED_FORMAT.value: "不支持的格式",
    IssueCode.UNPARSEABLE_AUDIO.value: "无法解析的音频",
    IssueCode.NO_LYRICS.value: "无任何歌词",
    IssueCode.MISSING_EMBEDDED.value: "缺内嵌歌词",
    IssueCode.MISSING_SIDECAR.value: "缺 sidecar",
    IssueCode.PLAIN_EMBEDDED_LYRICS.value: "无时间轴的内嵌歌词",
    IssueCode.SUSPICIOUS_LYRICS.value: "可疑歌词（占位/越界等）",
    IssueCode.LYRICS_MISMATCH.value: "内嵌与 sidecar 内容不一致",
    IssueCode.NO_EMBEDDED_COVER.value: "无内嵌封面",
    IssueCode.NO_COVER_ARTWORK.value: "无任何封面",
    IssueCode.MISSING_MBID.value: "缺 MusicBrainz ID",
    IssueCode.MISSING_ISRC.value: "缺 ISRC",
    IssueCode.MISSING_TITLE.value: "缺标题",
    IssueCode.MISSING_ARTIST.value: "缺艺术家",
    IssueCode.MISSING_ALBUM.value: "缺专辑",
}


def render_stats_text(stats: LibraryStats) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 66)
    add(f"Lyra 曲库统计  root={stats.root_name}  {stats.root_path}")
    add("=" * 66)
    add(f"音乐文件总数      {stats.audio_total}")
    add(f"LRC 歌词文件总数  {stats.lrc_total}")
    add("")
    add("-- 音乐文件（按真实容器）--")
    for name, count in sorted(stats.by_format.items(), key=lambda kv: -kv[1]):
        pct = count * 100 / max(stats.audio_total, 1)
        add(f"  {_FORMAT_LABEL.get(name, name):16} {count:6}  {pct:5.1f}%")
    if stats.parse_failures:
        add(f"  {'解析失败':16} {stats.parse_failures:6}")

    add("")
    add("-- LRC 歌词文件（按内容）--")
    add(f"  真正有歌词(带时间轴)  {stats.lrc_synced:6}")
    add(f"  纯文本(无时间轴)      {stats.lrc_plain:6}")
    add(f"  可疑(占位/越界等)     {stats.lrc_suspicious:6}")
    add(f"  无法解析              {stats.lrc_unparseable:6}")
    add(f"  0 字节空文件          {stats.lrc_empty:6}   ← 占位残留，可安全填充")
    add(f"  带 BOM                {stats.lrc_bom:6}")
    add(f"  孤儿(无对应音频)      {stats.lrc_orphan:6}")
    add(f"  疑似重复组            {stats.lrc_duplicate_groups:6}")

    add("")
    add("-- 内嵌歌词 --")
    add(f"  带时间轴              {stats.embedded_synced:6}")
    add(f"  可疑                  {stats.embedded_suspicious:6}")
    add(f"  纯文本(无时间轴)      {stats.embedded_plain:6}")
    add(f"  无内嵌歌词            {stats.embedded_absent:6}")
    add(f"  无内嵌封面            {stats.without_embedded_cover:6}")

    add("")
    add("-- .lrc 覆盖（播放器实际读的是这份）--")
    add(f"  带时间轴 .lrc {stats.sidecar_timed} / {stats.audio_total}"
        f"   →  {stats.sidecar_coverage_pct}%")
    add(f"    其中：有 .lrc 无时间轴 {stats.sidecar_untimed}"
        f"｜缺 .lrc 但内嵌有词 {stats.sidecar_missing_embedded}"
        f"｜两者都没有 {stats.sidecar_missing_none}")
    add("")
    add("-- 内嵌歌词覆盖 --")
    add(f"  带时间轴 {stats.embedded_synced} / {stats.audio_total}"
        f"   →  {stats.embedded_coverage_pct}%")
    add(f"  任一形式有歌词 {stats.tracks_with_lyrics} / {stats.audio_total}"
        f"   →  {stats.coverage_pct}%")

    if stats.albums:
        hours = stats.total_seconds / 3600
        add("")
        add("-- 规模与标签 --")
        add(f"  总时长 {hours:.1f} 小时｜专辑 {stats.album_total} 张"
            f"｜歌手署名 {stats.artist_total} 个｜流派 {stats.genre_total} 种")
        add("  流派分布：" + "，".join(
            f"{item['name']} {item['count']}" for item in stats.genres[:6]))
        add("  年代分布：" + "，".join(
            f"{item['name']} {item['count']}" for item in stats.decades))
        add("  语种分布：" + "，".join(
            f"{item['name']} {item['count']}" for item in stats.languages))

    if stats.issues:
        add("")
        add("-- 问题分布 --")
        for name, count in sorted(stats.issues.items(), key=lambda kv: -kv[1]):
            add(f"  {_ISSUE_LABEL.get(name, name):26} {count}")

    add("")
    add(f"扫描耗时 {stats.elapsed_seconds:.2f}s")
    return "\n".join(lines)
