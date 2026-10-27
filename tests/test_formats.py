"""格式识别与歌词字段白名单的测试。

为什么测这些：

1. **必须以真实容器为准，不能按扩展名判断。** 目标曲库里实际有
   `蝶恋花.flac` / `Live/Trophy Boy.flac` 两个文件**实际是 OGG 容器**。
   按扩展名判断会让解析直接抛错、或更糟——把 OGG 当 FLAC 去改写而毁掉文件。

2. **歌词字段必须用精确白名单。** `LYRICIST`（Vorbis）与 ID3 的 `TEXT` 帧是
   **作词人**，不是歌词。若用 `"lyric" in field_name` 之类的子串匹配，
   清理「无时间轴歌词」时会把作词人一起删掉——那是不可恢复的元数据损失。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.formats import (
    ID3_LYRICS_FRAMES,
    VORBIS_LYRICS_FIELDS,
    detect_media_format,
    read_audio_meta,
)
from lyra.models import LyricsState, MediaFormat

# ---------------------------------------------------------------------------
# 真实文件的格式识别
# ---------------------------------------------------------------------------


def test_真实FLAC被识别为FLAC(real_flac: Path, tmp_path: Path):
    fmt, handle, error = detect_media_format(real_flac)
    assert error is None
    assert fmt is MediaFormat.FLAC
    assert handle is not None


def test_真实MP3被识别为MP3(real_mp3: Path):
    fmt, handle, error = detect_media_format(real_mp3)
    assert error is None
    assert fmt is MediaFormat.MP3
    assert handle is not None


def test_真实文件扩展名与容器一致(real_flac: Path, real_mp3: Path, tmp_path: Path):
    """正常文件的 ext_match 必须为 True，否则会把整个库都报成异常。"""
    for src in (real_flac, real_mp3):
        meta = read_audio_meta(src, src.parent)
        assert meta.info.extension_matches_container is True
        assert meta.info.parse_error is None


def test_读取真实文件能拿到时长与非空标签(real_flac: Path):
    """时长是匹配的核心判据（时长闸门 ±2s），拿不到时长等于无法准确刮削。"""
    meta = read_audio_meta(real_flac, real_flac.parent)
    assert meta.info.duration is not None and meta.info.duration > 0
    assert meta.tags, "真实 FLAC 应当能读出标签"
    assert any(key in meta.tags for key in ("TITLE", "ARTIST", "ALBUM"))


# ---------------------------------------------------------------------------
# 合成 Ogg：容器与扩展名不符
# ---------------------------------------------------------------------------


def test_正常命名的OGG被识别为OGG(ogg_factory, tmp_path: Path):
    """基线：内容与扩展名一致时，Ogg 能被正确识别。"""
    path = ogg_factory(tmp_path / "song.ogg", entries=[("TITLE", "标题")])
    fmt, handle, error = detect_media_format(path)
    assert error is None
    assert fmt is MediaFormat.OGG_VORBIS

    meta = read_audio_meta(path, tmp_path)
    assert meta.info.media_format is MediaFormat.OGG_VORBIS
    assert meta.info.extension_matches_container is True
    assert meta.info.duration == pytest.approx(3.0, abs=0.1)


def test_OGG改名为flac仍应被识别为OGG(ogg_factory, tmp_path: Path):
    """D10 核心：必须以**内容**定容器，而不是被 mutagen 的扩展名打分牵着走。

    曲库里真实存在 `蝶恋花.flac` / `Live/Trophy Boy.flac` 两个 OGG 冒充 FLAC 的文件。
    早期实现用 mutagen.File(path) 探测，扩展名为 .flac 时只会尝试 FLAC 解析器，
    该解析器对非 fLaC 内容抛 FLACNoHeaderError 并直接向上传播、不回退到内容嗅探，
    于是这两个文件被报成 unsupported + parse_error，永远无法被正确处理。
    现在改为「先嗅探容器、再用对应类显式打开」，这条测试锁死该行为不许回退。
    """
    path = ogg_factory(tmp_path / "fake.flac", entries=[("TITLE", "标题")])
    fmt, handle, error = detect_media_format(path)
    assert fmt is MediaFormat.OGG_VORBIS, f"实际得到 {fmt} / {error}"
    assert error is None
    assert handle is not None

    meta = read_audio_meta(path, tmp_path)
    assert meta.info.media_format is MediaFormat.OGG_VORBIS
    # 这是唯一能让审计报出 CONTAINER_MISMATCH 的信号，不能丢
    assert meta.info.extension_matches_container is False
    # 认得出容器就要能读时长——时长是匹配阶段的核心判据
    assert meta.info.duration == pytest.approx(3.0, abs=0.1)
    assert meta.info.parse_error is None


def test_OGG改名为flac绝不能被当成FLAC(ogg_factory, tmp_path: Path):
    """安全不变量：任何情况下都不能把 OGG 误判为 FLAC。

    把 OGG 当 FLAC 去改写会直接毁掉音频数据，比「拒绝处理」严重得多。
    这条不依赖具体实现路径，锁住的是「宁可拒绝，不可猜错」。
    """
    path = ogg_factory(tmp_path / "fake.flac")
    fmt, _, _ = detect_media_format(path)
    assert fmt is not MediaFormat.FLAC

    meta = read_audio_meta(path, tmp_path)
    assert meta.info.media_format is not MediaFormat.FLAC
    assert meta.info.extension_matches_container is False


# ---------------------------------------------------------------------------
# 歌词字段白名单：LYRICIST 不是歌词
# ---------------------------------------------------------------------------


def test_内嵌歌词只认LYRICS而不是LYRICIST(flac_factory, tmp_path: Path):
    """这是数据保护的核心断言：清理歌词时绝不能动到作词人。"""
    path = flac_factory(
        tmp_path / "song.flac",
        entries=[
            ("TITLE", "某歌"),
            ("ARTIST", "某歌手"),
            ("LYRICIST", "某作词人"),
            ("LYRICS", "[00:01.00]第一行\n[00:02.00]第二行"),
        ],
    )
    meta = read_audio_meta(path, tmp_path)

    fields = [f.upper() for f in meta.embedded.fields]
    assert fields == ["LYRICS"], f"命中的歌词字段应是 ['LYRICS']，实际 {fields}"
    assert "LYRICIST" not in fields
    # 作词人必须仍然作为普通标签可读——它只是「不算歌词」，不该消失
    assert meta.tags.get("LYRICIST") == ["某作词人"]
    assert meta.embedded.state is LyricsState.SYNCED


def test_只有LYRICIST时判为没有歌词(flac_factory, tmp_path: Path):
    """反例：只有作词人字段时必须判为 absent，否则这首歌永远不会去刮歌词。"""
    path = flac_factory(
        tmp_path / "song.flac",
        entries=[("TITLE", "某歌"), ("LYRICIST", "某作词人")],
    )
    meta = read_audio_meta(path, tmp_path)
    assert meta.embedded.state is LyricsState.ABSENT
    assert meta.embedded.fields == ()
    assert meta.tags.get("LYRICIST") == ["某作词人"]


def test_UNSYNCEDLYRICS也被认作歌词(flac_factory, tmp_path: Path):
    """白名单包含 UNSYNCEDLYRICS；它虽名为 unsynced，内容仍可能是带时间轴的。"""
    path = flac_factory(
        tmp_path / "song.flac",
        entries=[("TITLE", "某歌"), ("UNSYNCEDLYRICS", "[00:01.00]只有一行")],
    )
    meta = read_audio_meta(path, tmp_path)
    fields = [f.upper() for f in meta.embedded.fields]
    assert fields == ["UNSYNCEDLYRICS"]
    assert meta.embedded.state is LyricsState.SYNCED


def test_白名单常量本身不含作词人字段():
    """常量被读与写共用；它一旦被改宽，读写两侧会同时开始破坏作词人。"""
    assert frozenset({"LYRICS", "UNSYNCEDLYRICS"}) == VORBIS_LYRICS_FIELDS
    for lookalike in ("LYRICIST", "TEXT", "COMPOSER", "TCOM", "SONGWRITER", "WRITER"):
        assert lookalike not in VORBIS_LYRICS_FIELDS
    assert ID3_LYRICS_FRAMES == ("USLT", "SYLT")
    assert "TEXT" not in ID3_LYRICS_FRAMES


def test_多个歌词值都会被读取(flac_factory, tmp_path: Path):
    """设计约束 要求读取所有歌词值，不能只读第一个。"""
    path = flac_factory(
        tmp_path / "song.flac",
        entries=[
            ("LYRICS", "只有纯文本"),
            ("LYRICS", "[00:01.00]带时间轴的"),
        ],
    )
    meta = read_audio_meta(path, tmp_path)
    # 取时间轴质量最高者：synced 优于 plain_text
    assert meta.embedded.state is LyricsState.SYNCED
    assert meta.embedded.parsed is not None
    assert meta.embedded.parsed.line_count == 1


# ---------------------------------------------------------------------------
# ID3：TEXT（作词人）不是歌词
# ---------------------------------------------------------------------------


def _with_id3(path: Path, *, text: str | None = None, uslt: str | None = None) -> Path:
    from mutagen.id3 import ID3, TEXT, USLT

    tag = ID3()
    if text is not None:
        tag.add(TEXT(encoding=3, text=[text]))
    if uslt is not None:
        tag.add(USLT(encoding=3, lang="XXX", desc="", text=uslt))
    tag.save(path)
    return path


def test_ID3的TEXT帧不被当作歌词(real_mp3: Path, tmp_path: Path):
    """TEXT 是「作词人」帧。只有它时必须判为没有歌词，且帧本身要保留。"""
    _with_id3(real_mp3, text="某作词人")
    meta = read_audio_meta(real_mp3, real_mp3.parent)

    assert meta.info.media_format is MediaFormat.MP3
    assert meta.embedded.state is LyricsState.ABSENT, "TEXT 被当成了歌词"
    assert meta.embedded.fields == ()

    # 帧必须仍在文件里——判定「不是歌词」不等于可以删掉它
    from mutagen.id3 import ID3

    assert len(ID3(real_mp3).getall("TEXT")) == 1


def test_ID3的USLT才是歌词(real_mp3: Path, tmp_path: Path):
    """USLT 是歌词帧；同时存在 TEXT 时也只应命中 USLT。"""
    _with_id3(real_mp3, text="某作词人", uslt="[00:01.00]第一行\n[00:02.00]第二行")
    meta = read_audio_meta(real_mp3, real_mp3.parent)

    assert meta.embedded.state is LyricsState.SYNCED
    assert all(f.startswith("USLT") for f in meta.embedded.fields), meta.embedded.fields
    assert not any("TEXT" in f for f in meta.embedded.fields)


# ---------------------------------------------------------------------------
# 损坏与不支持的输入
# ---------------------------------------------------------------------------


def test_非音频文件返回unsupported而不抛异常(tmp_path: Path):
    """损坏文件不能让整次扫描崩掉；必须降级为 unsupported + 原因。"""
    path = tmp_path / "broken.flac"
    path.write_bytes(b"this is definitely not audio" * 10)

    fmt, handle, error = detect_media_format(path)
    assert fmt is MediaFormat.UNSUPPORTED
    assert handle is None
    assert error

    meta = read_audio_meta(path, tmp_path)
    assert meta.info.media_format is MediaFormat.UNSUPPORTED
    assert meta.info.parse_error is not None
    assert meta.tags == {}, "解析失败时不应返回任何标签"


def test_空文件不抛异常(tmp_path: Path):
    path = tmp_path / "empty.flac"
    path.write_bytes(b"")
    meta = read_audio_meta(path, tmp_path)
    assert meta.info.media_format is MediaFormat.UNSUPPORTED
    assert meta.info.parse_error is not None


def test_rel_path相对于受管根(flac_factory, tmp_path: Path):
    """rel_path 必须具备可移植性：相对受管根，而不是绝对路径。"""
    path = flac_factory(tmp_path / "lib" / "album" / "01 song.flac")
    meta = read_audio_meta(path, tmp_path / "lib")
    assert meta.info.rel_path == str(Path("album") / "01 song.flac")


def test_rel_path在根之外时退化为原路径(flac_factory, tmp_path: Path):
    """文件不在根下时不能抛异常，否则一次误配置会让扫描整体失败。"""
    path = flac_factory(tmp_path / "outside" / "song.flac")
    meta = read_audio_meta(path, tmp_path / "somewhere-else")
    assert meta.info.rel_path == str(path)


# ---------------------------------------------------------------------------
# MP3 / OGG 的标签读取
# ---------------------------------------------------------------------------


def test_mp3能读出标签(tmp_path):
    """实际缺陷：`read_all_tags` 只实现了 FLAC，MP3/OGG 直接返回
    「暂不支持读取……的全部标签」，于是标签修复功能对这两类文件完全不可用。

    而 `id3meta` 的解析器早就存在（写入器一直在用），只是没人把
    帧解成文本。
    """
    from lyra.formats import read_all_tags

    # 构造一个最小 ID3v2.3 标签：TIT2(标题) + TPE1(艺人)
    def frame(fid, text):
        body = b"\x00" + text.encode("latin-1")          # 编码 0 = Latin-1
        size = len(body)
        # ID3v2.3 帧大小是普通 32 位大端
        return fid.encode() + size.to_bytes(4, "big") + b"\x00\x00" + body

    frames = frame("TIT2", "Test Title") + frame("TPE1", "Test Artist")
    tag = b"ID3\x03\x00\x00" + len(frames).to_bytes(4, "big") + frames
    path = tmp_path / "sample.mp3"
    path.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 64)

    tags, err = read_all_tags(path)
    assert err is None, f"读 MP3 标签不该报错：{err}"
    assert tags.get("TITLE") == ["Test Title"], tags
    assert tags.get("ARTIST") == ["Test Artist"], tags


def test_标签读取不再对mp3ogg一律报错(tmp_path):
    """回归：这两种容器的读取路径必须存在，而不是直接返回「暂不支持」。"""
    import inspect

    from lyra import formats

    src = inspect.getsource(formats.read_all_tags)
    assert "OGG_VORBIS" in src and "OGG_OPUS" in src, "OGG 应走 Vorbis comment 读取"
    assert "MP3" in src and "_read_id3_tags" in src, "MP3 应走 ID3 读取"


def test_mp3的匹配标签也能读出(tmp_path):
    """回归：**MP3 的标签曾一个都读不出来**。

    根因是这里拿 Vorbis 风格的名字（`TITLE`）去查 mutagen 的 ID3 对象，
    而 ID3 的键是帧 ID（`TIT2`）——永远查不到。

    后果不只是健康页误报「缺标题」，更严重的是**匹配时标题与艺人为空**，
    那首 MP3 因此永远找不到歌词。
    """
    from lyra.formats import MediaFormat, _read_match_tags

    class FakeFrame:
        def __init__(self, text):
            self.text = [text]

    class FakeID3(dict):
        pass

    class FakeHandle:
        def __init__(self):
            self.tags = FakeID3({
                "TIT2": FakeFrame("Test Title"),
                "TPE1": FakeFrame("Test Artist"),
                "TALB": FakeFrame("Test Album"),
            })

    out = _read_match_tags(FakeHandle(), MediaFormat.MP3)
    assert out.get("TITLE") == ["Test Title"], out
    assert out.get("ARTIST") == ["Test Artist"], out
    assert out.get("ALBUM") == ["Test Album"], out


def test_标签名到ID3帧的映射存在():
    from lyra.formats import _ID3_FRAME_FOR_NAME

    assert _ID3_FRAME_FOR_NAME["TITLE"] == ("TIT2",)
    assert _ID3_FRAME_FOR_NAME["ARTIST"] == ("TPE1",)
    # 年份有两个可能的帧，两个都要认
    assert set(_ID3_FRAME_FOR_NAME["DATE"]) == {"TDRC", "TYER"}


def test_mp3_musicbrainz_recording_id_is_decoded(tmp_path: Path) -> None:
    """UFID stores raw bytes; it must become a UUID string, not ``b'...'``."""
    from mutagen.id3 import ID3, UFID

    source = Path(__file__).parent / "fixtures" / "audio" / "sample_v23.mp3"
    target = tmp_path / "mbid.mp3"
    target.write_bytes(source.read_bytes())
    tags = ID3(target)
    tags.add(UFID(owner="http://musicbrainz.org", data=b"0b29b591-06e6-5c22-9e06-e987fa9c3dc9"))
    tags.save(target)
    meta = read_audio_meta(target, tmp_path)
    assert meta.tags.get("MUSICBRAINZ_TRACKID") == [
        "0b29b591-06e6-5c22-9e06-e987fa9c3dc9"
    ]


def test_mp3能读出TXXX写法的MBID(tmp_path: Path) -> None:
    """Picard 等工具把 MBID 写成 TXXX 帧——那才是社区通行的写法。

    实际后果：只认 UFID 的话，明明写了 MBID 的 MP3 会被报成「缺 MBID」。
    """
    from mutagen.id3 import ID3, TXXX

    source = Path(__file__).parent / "fixtures" / "audio" / "sample_v23.mp3"
    target = tmp_path / "txxx_mbid.mp3"
    target.write_bytes(source.read_bytes())
    tags = ID3(target)
    tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id",
                  text="02df1d18-56a6-4ad6-9568-4352bf55d313"))
    tags.save(target)
    meta = read_audio_meta(target, tmp_path)
    assert meta.tags.get("MUSICBRAINZ_TRACKID") == [
        "02df1d18-56a6-4ad6-9568-4352bf55d313"
    ]
