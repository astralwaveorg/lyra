"""「粘一条 MusicBrainz 链接就把 MBID 补上」的测试。

## 为什么单独一个文件

这是**第二条**接受用户输入的路径（第一条是歌词链接），而且它的输入
直接决定往文件里写什么字段。三类错误都必须被固定住：

1. 链接判定太松：`musicbrainz.org.evil.com` 也认，等于任何域名都能
   指定「来源」。这不是凭据泄漏，但会让人以为数据来自 MusicBrainz。
2. 挑错发行：同一首录音同时挂在单曲与合辑上（曲库真实发生过）。选错那张
   会把 ALBUMID / RELEASEGROUPID / RELEASETRACKID 全部写成另一张发行的，
   **比不填更糟**——它看起来是完整的。
3. 猜错曲目：没有 MBID 锚点时按标题+时长定位曲目，两边都不像就必须留空，
   绝对不能「拿第一条凑数」，否则曲序会写到同一张专辑的别的歌上。

下面用的 JSON 是**照着本地镜像的真实响应手抄的**（不是编的），
键名、嵌套层数、`media[].tracks[]` 与 `media[].track` 两种形态都保留。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import lyra.api as api_module
from lyra.api import create_app
from lyra.audit import audit_root
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.db import open_store
from lyra.models import RootRole
from lyra.pipeline import sync_state
from lyra.tagsources import (
    EntityLink,
    MbidLinkError,
    MusicBrainzConfig,
    MusicBrainzProvider,
    lookup_by_link,
    lookup_by_release,
    parse_entity_link,
)

# --- 真实 MBID（曲库里那首《我们的梦》） ------------------------------------
RECORDING = "449ff670-1277-4245-8cd9-7dfe42698b54"
RELEASE_SINGLE = "e8802aae-7310-461a-9eef-4b79c56f95e4"      # 我們的夢 / Single
RELEASE_ALBUM = "e063e624-451a-4901-8eb6-84e219a03d80"       # in my singk feels
RG_SINGLE = "9f13866d-6836-4ab4-94d8-03efeef5dc06"
RG_ALBUM = "0721353c-32f2-4971-b600-8ee6d831739f"
RELEASE_TRACK_IN_ALBUM = "7b868349-e328-4a2b-9b01-a3407cb268e2"
ARTIST = "52f243b5-cca7-4ba5-b32e-125096c7646e"

RECORDING_DETAIL = {
    "id": RECORDING,
    "title": "我們的夢",
    "length": 202679,
    "isrcs": ["HKUM71800096"],
    "artist-credit": [
        {"name": "譚詠麟", "joinphrase": "、", "artist": {"id": ARTIST, "name": "譚詠麟"}},
        {"name": "鍾鎮濤", "artist": {"id": "fcb0061b-f819-4792-a255-57b93014c13d",
                                     "name": "鍾鎮濤"}},
    ],
    "releases": [
        {"id": RELEASE_SINGLE, "title": "我們的夢", "date": "2018-05-08",
         "status": "Official", "country": "XW",
         "release-group": {"id": RG_SINGLE, "primary-type": "Single"}},
        {"id": RELEASE_ALBUM, "title": "in my singk feels", "date": "2026-02-06",
         "status": "Official", "country": "XW",
         "release-group": {"id": RG_ALBUM, "primary-type": "Album"}},
    ],
}

RELEASE_SINGLE_JSON = {
    "id": RELEASE_SINGLE,
    "title": "我們的夢",
    "date": "2018-05-08",
    "country": "XW",
    "status": "Official",
    "release-group": {"id": RG_SINGLE},
    "artist-credits": [
        {"name": "譚詠麟", "artist": {"id": ARTIST}},
        {"joinphrase": "、"},
        {"name": "鍾鎮濤", "artist": {"id": "fcb0061b-f819-4792-a255-57b93014c13d"}},
    ],
    "media": [{
        "position": 1, "format": "Digital Media", "track-count": 1,
        "tracks": [{
            "id": "a836704d-2baf-4087-afa3-45b89a13960f",
            "number": "1", "title": "我們的夢", "length": 202679, "position": 1,
            "recording": {"id": RECORDING, "title": "我們的夢"},
        }],
    }],
}

RELEASE_ALBUM_JSON = {
    "id": RELEASE_ALBUM,
    "title": "in my singk feels",
    "date": "2026-02-06",
    "country": "XW",
    "status": "Official",
    # 这张合辑**没有** artist-credit（实际本地镜像就是这样），
    # 所以绝不能因为「来源没给」就把本地 ALBUMARTIST 抹掉。
    "artist-credits": [],
    "label-info": [{"label": {"name": "uDiscover Music"}, "catalog-number": None}],
    "release-group": {"id": RG_ALBUM},
    "media": [{
        "position": 1, "format": "Digital Media", "track-count": 20,
        "tracks": [
            {"id": "1a285f93-21ed-479d-8ced-11861f33a067", "number": "1",
             "title": "只是太愛你", "length": 248800, "position": 1,
             "recording": {"id": "15b36f38-bacf-46a6-ad60-92eb7b822cad",
                           "title": "只是太愛你"}},
            {"id": RELEASE_TRACK_IN_ALBUM, "number": "2",
             "title": "我們的夢", "length": 202679, "position": 2,
             "recording": {"id": RECORDING, "title": "我們的夢"}},
            {"id": "d6352249-824b-4d4e-9b70-44bd3d3653c0", "number": "3",
             "title": "迷路在雲端", "length": 303745, "position": 3,
             "recording": {"id": "a9fc8b97-ede8-4b08-90bd-c3c842c8ca32",
                           "title": "迷路在雲端"}},
        ],
    }],
}


def _handler(request: httpx.Request) -> httpx.Response:
    """按路径分发，模拟本地镜像。"""
    path = request.url.path
    if path == f"/ws/2/recording/{RECORDING}":
        return httpx.Response(200, json=RECORDING_DETAIL)
    if path == f"/ws/2/release/{RELEASE_SINGLE}":
        return httpx.Response(200, json=RELEASE_SINGLE_JSON)
    if path == f"/ws/2/release/{RELEASE_ALBUM}":
        return httpx.Response(200, json=RELEASE_ALBUM_JSON)
    if path == f"/ws/2/release-group/{RG_ALBUM}":
        return httpx.Response(200, json={
            "id": RG_ALBUM, "title": "in my singk feels",
            "releases": [{"id": RELEASE_ALBUM, "title": "in my singk feels",
                          "date": "2026-02-06"}],
        })
    if path == f"/ws/2/release-group/{RG_SINGLE}":
        return httpx.Response(200, json={
            "id": RG_SINGLE, "title": "我們的夢",
            "releases": [{"id": RELEASE_SINGLE, "title": "我們的夢",
                          "date": "2018-05-08"}],
        })
    if path == f"/ws/2/artist/{ARTIST}":
        return httpx.Response(200, json={"id": ARTIST, "name": "譚詠麟"})
    return httpx.Response(404, json={"error": f"未预置的端点: {path}"})


@pytest.fixture
def provider() -> MusicBrainzProvider:
    mb = MusicBrainzProvider(
        MusicBrainzConfig(base_url="http://mb.test", minimum_request_interval_seconds=0.0),
        transport=httpx.MockTransport(_handler),
        sleep=lambda _s: None,
    )
    yield mb
    mb.close()


# ---------------------------------------------------------------------------
# 链接解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,entity,mbid", [
    (f"https://musicbrainz.org/recording/{RECORDING}", "recording", RECORDING),
    (f"http://musicbrainz.org/release/{RELEASE_ALBUM}/", "release", RELEASE_ALBUM),
    (f"musicbrainz.org/release-group/{RG_ALBUM}", "release-group", RG_ALBUM),
    (f"https://beta.musicbrainz.org/artist/{ARTIST}?inc=releases", "artist", ARTIST),
    (f"https://musicbrainz.org/ws/2/recording/{RECORDING}?fmt=json", "recording", RECORDING),
    # MusicBrainz 的 ID 大小写无关，页面地址可能带大写
    (f"https://musicbrainz.org/recording/{RECORDING.upper()}", "recording", RECORDING),
])
def test_能从各种写法里认出条目(text, entity, mbid):
    link = parse_entity_link(text)
    assert isinstance(link, EntityLink)
    assert (link.entity, link.mbid) == (entity, mbid)


@pytest.mark.parametrize("text,hint", [
    ("", "没有填入链接"),
    ("https://example.com/recording/" + RECORDING, "musicbrainz.org"),
    # 域名必须按边界判断：这是最容易写错的一种
    ("https://musicbrainz.org.evil.example/recording/" + RECORDING, "musicbrainz.org"),
    ("https://musicbrainz.org/", "没有 ID"),
    ("https://musicbrainz.org/track/" + RECORDING, "不支持的条目类型"),
    ("https://musicbrainz.org/recording/not-a-uuid", "UUID"),
])
def test_不是条目链接时说清原因(text, hint):
    with pytest.raises(MbidLinkError) as excinfo:
        parse_entity_link(text)
    assert hint in str(excinfo.value)


# ---------------------------------------------------------------------------
# 链接 → 候选
# ---------------------------------------------------------------------------


def test_录音链接列出它出现的每一张发行(provider):
    """同一首录音挂在单曲与合辑上时**两条都要给**，由人来定哪张是本地的。"""
    cands = lookup_by_link(
        provider,
        parse_entity_link(f"https://musicbrainz.org/recording/{RECORDING}"),
        title="我们的梦", duration=202.679, album="in my singk feels",
        album_id=RELEASE_SINGLE,   # 本地 ALBUMID 恰好指错了那张（实际如此）
    )
    assert len(cands) == 2
    assert [c.fields["MUSICBRAINZ_ALBUMID"][0] for c in cands] == [
        RELEASE_ALBUM, RELEASE_SINGLE,
    ], "与本地专辑名一致的那张必须排前面"

    album = cands[0]
    assert album.fields["ALBUM"] == ["in my singk feels"]
    assert album.fields["MUSICBRAINZ_TRACKID"] == [RECORDING]
    assert album.fields["MUSICBRAINZ_ALBUMID"] == [RELEASE_ALBUM]
    assert album.fields["MUSICBRAINZ_RELEASEGROUPID"] == [RG_ALBUM]
    assert album.fields["MUSICBRAINZ_RELEASETRACKID"] == [RELEASE_TRACK_IN_ALBUM]
    assert album.fields["TRACKNUMBER"] == ["2"]
    assert album.fields["DISCNUMBER"] == ["1"]
    assert album.fields["DATE"] == ["2026-02-06"]
    assert album.fields["LABEL"] == ["uDiscover Music"]
    assert album.fields["ISRC"] == ["HKUM71800096"]
    # 两个信号互相矛盾时必须**说出来**，而不是偷偷按某一个决定
    assert album.extra["本地对照"] == "专辑名一致，MBID 不同"
    # 来源没给 artist-credit 的合辑不能凭空写 ALBUMARTIST
    assert "ALBUMARTIST" not in album.fields

    single = cands[1]
    assert single.fields["MUSICBRAINZ_ALBUMID"] == [RELEASE_SINGLE]
    assert single.fields["TRACKNUMBER"] == ["1"]
    assert single.extra["本地对照"] == "MBID 一致，专辑名不同"


def test_发行链接按录音锚点定位曲目(provider):
    cands = lookup_by_link(
        provider,
        parse_entity_link(f"https://musicbrainz.org/release/{RELEASE_ALBUM}"),
        recording_id=RECORDING,
    )
    assert len(cands) == 1
    assert cands[0].fields["MUSICBRAINZ_RELEASETRACKID"] == [RELEASE_TRACK_IN_ALBUM]
    assert cands[0].fields["TRACKNUMBER"] == ["2"]


def test_没有锚点时按标题与时长定位(provider):
    """标签写着一半（只有 ALBUMID）时，靠标题+时长也能落在对的曲目上。"""
    cand = lookup_by_release(
        provider, release_id=RELEASE_ALBUM, title="我們的夢", duration=202.7,
    )
    assert cand is not None
    assert cand.fields["MUSICBRAINZ_RELEASETRACKID"] == [RELEASE_TRACK_IN_ALBUM]


def test_定位不到就留空而不是拿第一条凑数(provider):
    """猜错曲目会把曲序写到同专辑别的歌上——比不填严重得多。"""
    # 完全不像：标题不对、时长差得远
    cand = lookup_by_release(
        provider, release_id=RELEASE_ALBUM,
        title="这张专辑里根本没有这首", duration=999.0,
    )
    assert cand is not None
    assert "TRACKNUMBER" not in cand.fields
    assert "MUSICBRAINZ_RELEASETRACKID" not in cand.fields
    # 发行级字段仍然给出，不能让整条候选变空
    assert cand.fields["ALBUM"] == ["in my singk feels"]


def test_只有弱信号时不定位曲目(provider):
    """标题完全不符、时长差 4 秒——这不是「就是它」，是「碰巧接近」。

    阈值存在的意义就是这一档：去掉下限这段代码会静默把曲序写到别的歌上，
    而结果看起来完全正常（曲序、release-track-id 都填上了），最难发现。
    """
    cand = lookup_by_release(
        provider, release_id=RELEASE_ALBUM,
        title="毫不相干的标题", duration=244.8,   # 与第 1 首差 4.0 秒
    )
    assert cand is not None
    assert "TRACKNUMBER" not in cand.fields


def test_发行组链接展开成组内发行(provider):
    cands = lookup_by_link(
        provider, parse_entity_link(f"https://musicbrainz.org/release-group/{RG_ALBUM}"),
        recording_id=RECORDING,
    )
    assert [c.fields["MUSICBRAINZ_ALBUMID"][0] for c in cands] == [RELEASE_ALBUM]


def test_艺人链接只给艺人字段(provider):
    cands = lookup_by_link(
        provider, parse_entity_link(f"https://musicbrainz.org/artist/{ARTIST}"),
    )
    assert len(cands) == 1
    assert cands[0].fields["MUSICBRAINZ_ARTISTID"] == [ARTIST]
    assert cands[0].fields["ARTIST"] == ["譚詠麟"]
    assert "MUSICBRAINZ_TRACKID" not in cands[0].fields


def test_查不到的条目返回空列表而不是抛异常(provider):
    """链接有效但来源没有这条记录——是「没有结果」，不是「出错」。"""
    cands = lookup_by_link(
        provider,
        EntityLink(entity="release", mbid="00000000-0000-0000-0000-000000000000",
                   url=""),
    )
    assert cands == []


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, flac_factory, monkeypatch):
    lib = tmp_path / "lib"
    flac_factory(lib / "专辑" / "歌.flac", entries=(
        ("TITLE", "我们的梦"),
        ("ARTIST", "譚詠麟、鍾鎮濤"),
        ("ALBUM", "in my singk feels"),
        ("MUSICBRAINZ_ALBUMID", RELEASE_SINGLE),
    ))
    cfg = Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )
    cfg.providers.musicbrainz.base_url = "http://mb.test"

    def resync():
        store = open_store(cfg.storage.resolved_data_dir)
        try:
            sync_state(store, cfg, cfg.roots[0], audit_root(cfg.roots[0]))
        finally:
            store.close()

    resync()

    def factory(config):
        return MusicBrainzProvider(config, transport=httpx.MockTransport(_handler),
                                   sleep=lambda _s: None)

    monkeypatch.setattr(api_module, "MusicBrainzProvider", factory)

    app = create_app(cfg)
    with TestClient(app) as c:
        c.cfg = cfg        # type: ignore[attr-defined]
        c.lib = lib        # type: ignore[attr-defined]
        c.resync = resync  # type: ignore[attr-defined]
        yield c


def _link(env, url, path="专辑/歌.flac", root="lib"):
    return env.get("/api/v1/files/tags/link",
                   params={"root": root, "path": path, "url": url})


def test_接口按链接返回逐字段对比(env):
    r = _link(env, f"https://musicbrainz.org/recording/{RECORDING}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["link"]["entity"] == "recording"
    assert d["link"]["mbid"] == RECORDING
    assert len(d["candidates"]) == 2

    first = d["candidates"][0]
    assert first["fields"]["MUSICBRAINZ_ALBUMID"] == [RELEASE_ALBUM]
    rows = {row["key"]: row for row in first["rows"]}
    # 本地 ALBUM 一致 → same；MBID 指向另一张发行 → conflict 且不默认勾选
    assert rows["ALBUM"]["action"] == "same"
    assert rows["MUSICBRAINZ_ALBUMID"]["action"] == "conflict"
    assert rows["MUSICBRAINZ_RELEASETRACKID"]["action"] == "fill"
    assert rows["ISRC"]["action"] == "fill"
    assert first["summary"]["conflict"] >= 1


def test_接口拒绝非_MusicBrainz_链接(env):
    r = _link(env, "https://example.com/recording/" + RECORDING)
    assert r.status_code == 400
    assert "musicbrainz.org" in r.json()["detail"]


def test_接口拒绝越界路径(env):
    r = _link(env, f"https://musicbrainz.org/recording/{RECORDING}",
              path="../secret.flac")
    assert r.status_code in (400, 404)


def test_接口只接受音频文件(env):
    (env.lib / "专辑" / "歌.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    r = _link(env, f"https://musicbrainz.org/recording/{RECORDING}",
              path="专辑/歌.lrc")
    assert r.status_code == 415


def test_写入后缺陷立刻消失(env):
    """写完标签要**马上**重读这个文件的状态。

    否则要等监控的周期对账（30 秒）才更新，期间界面仍然挂着
    「缺 MusicBrainz ID」——实际会被当成「写了但没生效」。
    """
    before = env.get("/api/v1/tracks", params={"issue": "missing_mbid"}).json()
    assert before["total"] == 1, before

    r = env.post("/api/v1/files/tags", json={
        "root": "lib", "path": "专辑/歌.flac",
        "tags": {"MUSICBRAINZ_TRACKID": [RECORDING]},
    })
    assert r.status_code == 200, r.text
    assert r.json()["state_refreshed"] is True

    after = env.get("/api/v1/tracks", params={"issue": "missing_mbid"}).json()
    assert after["total"] == 0, "写入成功后这个缺陷必须立刻从列表里消失"


def test_写入时音频数据逐字节不变(env):
    from lyra.flacmeta import flac_audio_hash

    target = env.lib / "专辑" / "歌.flac"
    before = flac_audio_hash(target)
    r = env.post("/api/v1/files/tags", json={
        "root": "lib", "path": "专辑/歌.flac",
        "tags": {"MUSICBRAINZ_RELEASETRACKID": [RELEASE_TRACK_IN_ALBUM]},
    })
    assert r.status_code == 200
    assert flac_audio_hash(target) == before
