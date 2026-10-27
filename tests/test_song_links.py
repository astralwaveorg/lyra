"""链接解析：把歌曲页面地址变成（来源, 歌曲 id）。

## 为什么单独测

这条路径是「自动匹配失败时的唯一出路」，而它最容易出的错**不是崩溃**，
而是**静默解析出错误的东西**：把网易云的 id 当成 QQ 的 mid、把专辑 id
当成歌曲 id、把短链当成不支持的输入。错了以后的表现是「取回完全不相干的
歌词」，而界面上一切正常——这正是最该被测试钉住的失败模式。

所以这里的用例全部来自真实链接形状，并明确断言「拒绝」的情况。
"""

from __future__ import annotations

import pytest

from lyra.links import SongLink, UnsupportedLink, parse_song_link


@pytest.mark.parametrize(
    "url",
    [
        # 使用者实际给出的那一条
        "https://y.qq.com/n/ryqq_v2/songDetail/002APLOP0C614e",
        "https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e",
        "https://y.qq.com/n/yqq/song/002APLOP0C614e.html",
        "https://i.y.qq.com/v8/playsong.html?songmid=002APLOP0C614e&ADTAG=myqq",
        "https://c.y.qq.com/base/fcgi-bin/u?songmid=002APLOP0C614e",
        "y.qq.com/n/ryqq/songDetail/002APLOP0C614e",
    ],
)
def test_QQ各种链接形状都能解析(url):
    link = parse_song_link(url)
    assert link == SongLink("qq", "002APLOP0C614e", url)


@pytest.mark.parametrize(
    "url",
    [
        # 新版详情页给的是**数字歌曲 id**，不是 songmid
        "https://y.qq.com/n/ryqq_v2/songDetail/1839014",
        "https://y.qq.com/n/ryqq/songDetail/1839014",
        "https://y.qq.com/n/yqq/song/1839014.html",
        "https://i.y.qq.com/v8/playsong.html?songid=1839014",
    ],
)
def test_QQ数字歌曲id的链接也能解析(url):
    """实际：使用者贴 `https://y.qq.com/n/ryqq_v2/songDetail/1839014`
    时报「没有找到歌曲 id」——数字 id 不是 14 位 songmid，被规则挡掉了。"""
    link = parse_song_link(url)
    assert link == SongLink("qq", "1839014", url)


def test_显式写法也接受数字歌曲id():
    link = parse_song_link("qq:1839014")
    assert link == SongLink("qq", "1839014", "qq:1839014")


@pytest.mark.parametrize(
    "url",
    [
        "https://music.163.com/#/song?id=28341146",
        "https://music.163.com/song?id=28341146",
        "https://music.163.com/#/m/song?id=28341146",
        "https://y.music.163.com/m/song?id=28341146",
        "https://music.163.com/api/song/detail/?id=28341146",
        "https://music.163.com/#/song?id=28341146&userid=1",
    ],
)
def test_网易云各种链接形状都能解析(url):
    link = parse_song_link(url)
    assert link == SongLink("netease", "28341146", url)


def test_片段里的查询串也要认():
    """`#/song?id=123` 的查询串在 URL fragment 里，`urlparse` 看不到它。

    只解析 `?query` 的实现会漏掉整整一类网易云链接——而那是它最常见的形态。
    """
    raw = "https://music.163.com/#/song?id=28341146"
    assert parse_song_link(raw).song_id == "28341146"


def test_显式写法():
    assert parse_song_link("qq:002APLOP0C614e").provider == "qq"
    assert parse_song_link("netease:28341146").provider == "netease"
    assert parse_song_link("网易云：28341146").song_id == "28341146"


@pytest.mark.parametrize(
    "text",
    [
        "",                       # 空
        "002APLOP0C614e",         # 裸 id：来源不明，不能猜
        "28341146",
        "https://example.com/song?id=28341146",   # 未知域名
        "https://music.163.com/playlist?id=28341146",  # 歌单不是歌曲
        "https://y.qq.com/n/ryqq/albumDetail/002APLOP0C614e",
        "https://y.qq.com/n/ryqq/songDetail/notamid",
        # 数字歌曲 id 现在是合法的（见 test_显式写法也接受数字歌曲id），
        # 所以这里换成既不是 14 位 mid、也不是纯数字的写法
        "qq:abc",
        "netease:abc",
    ],
)
def test_无法识别时必须报错而不是猜(text):
    """宁可要求再贴一次，也不能解析出错误的东西去取歌词。"""
    with pytest.raises(UnsupportedLink):
        parse_song_link(text)


def test_报错信息要说清怎么办():
    with pytest.raises(UnsupportedLink) as e:
        parse_song_link("https://example.com/x")
    message = str(e.value)
    assert "y.qq.com" in message and "music.163.com" in message


def test_短链接给出可操作的提示():
    """短链需要联网跳转，这里明确说清而不是假装不支持这个平台。"""
    with pytest.raises(UnsupportedLink) as e:
        parse_song_link("https://163cn.tv/abcdefg")
    assert "短链接" in str(e.value)
    assert "地址栏" in str(e.value)


def test_歌单链接不会被当成歌曲():
    """`playlist?id=` 与 `song?id=` 只差一个词，猜错就会取回歌单里第一首的歌词。"""
    with pytest.raises(UnsupportedLink):
        parse_song_link("https://music.163.com/#/playlist?id=28341146")


def test_相似域名不会被当成官方域名():
    """`evilqq.com`.endswith("qq.com") 是 True——只判后缀等于放行任意域名。

    （`163.com` 的子域是合法的，所以只把「看起来像但不是子域」这种钉住。）
    """
    with pytest.raises(UnsupportedLink):
        parse_song_link("https://evilqq.com/n/ryqq/songDetail/002APLOP0C614e")
    with pytest.raises(UnsupportedLink):
        parse_song_link("https://qq.com.evil.net/n/ryqq/songDetail/002APLOP0C614e")
    # 真子域必须仍然可用
    assert parse_song_link("https://y.music.163.com/m/song?id=1").provider == "netease"


# ---------------------------------------------------------------------------
# QQ 音乐 App 的分享文本与短链接
# ---------------------------------------------------------------------------


#: 用户实际粘贴的内容：一整行分享文本，里面是 App 生成的短链。
QQ_SHARE_TEXT = (
    "G.E.M.邓紫棋《Victoria》 "
    "https://c6.y.qq.com/base/fcgi-bin/u?__=jYjQddQnqfxA @QQ音乐"
)

#: 该短链 302 之后的真实地址（实际抓到的跳转结果）。
QQ_SHARE_FINAL = (
    "https://i2.y.qq.com/n3/other/pages/playsong/index.html"
    "?ADTAG=cbshare&_wv=1&appshare=iphone&appsongtype=1"
    "&songmid=003BBDlj3apipE&source=qq&type=0"
)


def test_分享文本里的链接能被抠出来():
    """App 复制出来的是一整行 `歌名 链接 @QQ音乐`，直接当 URL 解析必然失败。"""
    link = parse_song_link(QQ_SHARE_TEXT, resolver=lambda _url: QQ_SHARE_FINAL)
    assert link.provider == "qq"
    assert link.song_id == "003BBDlj3apipE"


def test_短链接靠跟随跳转拿到歌曲_id():
    """`c6.y.qq.com/base/fcgi-bin/u?__=xxx` 的域名与路径都不含 id，
    只有 302 之后的地址里才有 songmid——这一步只能联网做。"""
    seen: list[str] = []

    def resolver(url: str) -> str:
        seen.append(url)
        return QQ_SHARE_FINAL

    link = parse_song_link(QQ_SHARE_TEXT, resolver=resolver)
    assert seen == ["https://c6.y.qq.com/base/fcgi-bin/u?__=jYjQddQnqfxA"], (
        "应当只把短链交给跳转器，而不是整段分享文本"
    )
    assert link.song_id == "003BBDlj3apipE"


def test_桌面版分享短链跳到数字id也能解析():
    """覆盖点：QQ 桌面版分享按钮给的是 `c6.y.qq.com/base/fcgi-bin/u?__=xxx`，
    跳转之后的地址是**新版详情页**（数字歌曲 id），不是老的 songmid 链接。"""
    seen: list[str] = []

    def resolver(url: str) -> str:
        seen.append(url)
        return (
            "https://y.qq.com/n/ryqq_v2/songDetail/1839014"
            "?ADTAG=h5_play_song&songtype=0&redirecttag=mn.redirect.custom&mnst=1.09"
        )

    link = parse_song_link(
        "https://c6.y.qq.com/base/fcgi-bin/u?__=U7J1hncp0Uwi", resolver=resolver
    )

    assert seen == ["https://c6.y.qq.com/base/fcgi-bin/u?__=U7J1hncp0Uwi"]
    assert link.provider == "qq"
    assert link.song_id == "1839014"


def test_没有跳转器时给出可照做的提示():
    """不注入 resolver 时不能假装成功，也不能只说「不支持」。"""
    with pytest.raises(UnsupportedLink) as e:
        parse_song_link(QQ_SHARE_TEXT)
    message = str(e.value)
    assert "跳转" in message and "地址栏" in message


def test_跳转没结果时说清可能是过期():
    with pytest.raises(UnsupportedLink) as e:
        parse_song_link(QQ_SHARE_TEXT, resolver=lambda url: url)
    assert "过期" in str(e.value)


def test_跳转之后的地址认不出来也要报错():
    with pytest.raises(UnsupportedLink):
        parse_song_link(QQ_SHARE_TEXT, resolver=lambda _url: "https://example.com/x")


def test_带_songmid_的同类路径不需要跳转():
    """`/base/fcgi-bin/u?songmid=xxx` 与 App 短链路径相同，但参数里已经有 id。

    只按路径判断「需要跳转」会让这种链接被无谓地走一遍网络，
    没有 resolver 时甚至会被拒——而它本来可以直接解析。
    """
    link = parse_song_link("https://c.y.qq.com/base/fcgi-bin/u?songmid=002APLOP0C614e")
    assert link.song_id == "002APLOP0C614e"


def test_用户给的跳转后链接直接可用():
    raw = (
        "https://y.qq.com/n/ryqq_v2/songDetail/003BBDlj3apipE"
        "?ADTAG=h5_play_song&songtype=0&redirecttag=mn.redirect.custom&mnst=0.98"
    )
    link = parse_song_link(raw)
    assert (link.provider, link.song_id) == ("qq", "003BBDlj3apipE")
