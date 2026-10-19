"""不能改写标签的格式，不该被报成「有问题」。

## 为什么必须有

`writer.apply_tag_edit` 只实现了 FLAC 的块级标签改写（音频数据必须逐字节
不变，OGG/MP3 的写法还没做），它对别的格式**明确拒绝**：
「暂不支持改写 ogg_vorbis 的标签（目前仅 FLAC）」。

但审计原来对 OGG/MP3 也照报「缺 MusicBrainz ID」这类缺陷，于是界面上出现
一个**没有出路**的问题项：点「修复」→ 填值 → 点「写入我填的值」→
得到一句「暂不支持改写」。用户的原话是「ogg 不支持改写，还提示错误，
也不应该提示存在问题」。

所以：不可写的格式不上报这些缺陷，但**也不能悄悄消失**——单独计数，
界面上说明「另有 N 项因格式不支持而无法修复」。
"""

from __future__ import annotations

from pathlib import Path

from lyra.audit import audit_root
from lyra.config import RootConfig
from lyra.models import IssueCode, MediaFormat, RootRole


def _cfg(tmp_path: Path, lib: Path) -> RootConfig:
    return RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))


def _issues(result) -> dict[str, set[str]]:
    return {
        t.audio.rel_path: {i.value for i in t.issues}
        for t in result.tracks
    }


def test_OGG_现在报标签类缺陷(tmp_path, ogg_factory):
    """OGG 的标签写入已实现，缺标题/艺人等必须照常上报。

    封面仍只支持 FLAC，因此 `no_embedded_cover` 不报，但会计入
    「因格式不支持而未计入」——修不了的事实不能凭空消失。
    """
    lib = tmp_path / "lib"
    ogg_factory(lib / "无标签.ogg")
    result = audit_root(_cfg(tmp_path, lib))
    issues = _issues(result)["无标签.ogg"]

    for code in ("missing_title", "missing_artist", "missing_album", "missing_mbid"):
        assert code in issues, f"OGG 现在能改标签，必须报 {code}"
    assert "no_embedded_cover" not in issues, "OGG 封面仍不可写"
    assert result.summary.issues_skipped_unfixable >= 1, result.summary


def test_四种格式都计入可写格式():
    """FLAC / MP3 / OGG Vorbis / OGG Opus 都有字节级标签写入器。"""
    from lyra.audit import TAG_WRITABLE_FORMATS

    assert frozenset(
        {
            MediaFormat.FLAC,
            MediaFormat.MP3,
            MediaFormat.OGG_VORBIS,
            MediaFormat.OGG_OPUS,
        }
    ) == TAG_WRITABLE_FORMATS


def test_FLAC_照常报标签类缺陷(tmp_path, flac_factory):
    """规则是按格式分的，不能一刀切把所有标签问题都关掉。"""
    lib = tmp_path / "lib"
    flac_factory(lib / "只有标题.flac", entries=(("TITLE", "歌"),))
    result = audit_root(_cfg(tmp_path, lib))
    issues = _issues(result)["只有标题.flac"]

    assert IssueCode.MISSING_MBID.value in issues, "FLAC 缺 MBID 必须照旧上报"
    assert IssueCode.MISSING_ARTIST.value in issues


def test_跳过计数会进统计(tmp_path, ogg_factory, flac_factory):
    """界面上要能说出「另有 N 项因为格式不支持而未计入」。"""
    from lyra.stats import collect_stats

    lib = tmp_path / "lib"
    ogg_factory(lib / "无标签.ogg")
    flac_factory(lib / "只有标题.flac", entries=(("TITLE", "歌"),))
    stats = collect_stats(_cfg(tmp_path, lib))
    assert stats.issues_skipped_unfixable >= 1
    assert "missing_mbid" in stats.issues, "FLAC 的缺陷仍在问题列表里"
    assert "missing_title" in stats.issues, "OGG 的标签缺陷现在也应可修"
