"""把「歌曲页面链接」解析成（来源, 歌曲 id）。

## 为什么需要它

自动匹配有两类情况**必然**失败，而且再怎么调参都救不回来：

1. 上游存在上传者冒名记录（标题、时长都对，歌手字段是上传者）；
2. 本地标签本身被写坏——实际 `06 G.E.M. 鄧紫棋 - 存在.flac` 的 TITLE
   标签是干净的，但 QQ 上同名歌曲一大堆（`存在` 有 20 多条不同录音），
   自动匹配只能按标题+时长猜，猜出来的全是别人的版本。

这时使用者**自己知道**答案。他能做的最省事的事就是复制一个链接。
所以这里的解析只做一件事：把链接变成「来源 + id」，余下的取词、写入
走既有的 `apply-lyrics`（同样只接受 provider + provider_id，正文由
服务端重新取回）。

## 设计取舍

- **解析本身不联网，但支持注入「短链跳转器」。** QQ 音乐 App 的分享链接
  （`c6.y.qq.com/base/fcgi-bin/u?__=xxx`）与 `163cn.tv/xxx` 都要跟随 302 才能
  拿到歌曲 id。纯解析做不到，所以这里接受一个 `resolver` 回调；不注入时
  就**明确报错并说清怎么办**，而不是假装能处理。
- **接受整段分享文本。** QQ 音乐 App 复制出来的是
  `G.E.M.邓紫棋《Victoria》 https://c6.y.qq.com/... @QQ音乐` 这样一整行，
  所以先从文本里把 URL 抠出来。
- **只认已知域名。** 不做「看起来像 id 就当 id」的猜测：一个 14 位的
  QQ mid 与一个纯数字的网易云 id 长得完全不同，但任何人都可能把别的
  数字串贴进来。域名不对就必须报错。
- 也接受 `qq:002APLOP0C614e` / `netease:28341146` 这种显式写法，
  方便从别处复制 id 的场景。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = ["SongLink", "UnsupportedLink", "parse_song_link", "SUPPORTED_LINK_HELP"]

#: 报错时给出的示例。使用者需要看到「什么才算合法输入」。
SUPPORTED_LINK_HELP = (
    "支持 QQ 音乐与网易云音乐的歌曲页面链接（含 App 分享出来的短链接），例如 "
    "https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e、"
    "https://c6.y.qq.com/base/fcgi-bin/u?__=jYjQddQnqfxA 或 "
    "https://music.163.com/#/song?id=28341146；"
    "也可以直接写 qq:002APLOP0C614e / netease:28341146。"
)


class UnsupportedLink(ValueError):
    """链接无法识别。消息里必须说清「为什么」与「该怎么办」。"""


@dataclass(frozen=True, slots=True)
class SongLink:
    provider: str
    """`qq` 或 `netease`。"""

    song_id: str
    original: str
    """使用者原本粘贴的内容，回显用。"""


#: QQ 的歌曲 id（songmid）是 14 位字母数字。
_QQ_MID = re.compile(r"^[0-9A-Za-z]{14}$")
_NUMERIC_ID = re.compile(r"^\d{1,20}$")

_QQ_HOST_SUFFIX = "qq.com"
_NETEASE_HOST_SUFFIX = "163.com"

#: 需要跟随跳转才能拿到 id 的短链域名 → 说明
_SHORT_LINK_HOSTS = {
    "163cn.tv": "网易云短链接需要跟随跳转才能得到歌曲 id",
    "url.cn": "QQ 音乐的短链接需要跟随跳转才能得到歌曲 id",
}

#: QQ 音乐 App 分享链接的路径。域名是普通的 qq.com 子域，只能靠路径认出来。
_QQ_SHARE_PATH = re.compile(r"/base/fcgi-bin/u(?:\?|$)", re.IGNORECASE)

#: 从任意文本里抠出第一个 http(s) 链接。
#: QQ 音乐 App 复制出来的是 `G.E.M.邓紫棋《Victoria》 https://... @QQ音乐`
#: 这样一整行，直接当成 URL 解析必然失败。
_URL_IN_TEXT = re.compile(r"https?://[^\s，。；、）)】\]]+", re.IGNORECASE)


def _host_of(raw: str) -> str:
    try:
        return (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _host_is(host: str, domain: str) -> bool:
    """域名匹配必须是「本身或子域」，不能只判后缀。

    `evilqq.com`.endswith("qq.com") 是 True——只判后缀等于把任意域名都放进来。
    这里没有凭据泄漏风险，但把无关链接当成 QQ 链接会取回错误的歌词，
    而表现是「看起来成功了」。
    """
    return host == domain or host.endswith("." + domain)


def _with_scheme(raw: str) -> str:
    """补上 scheme：浏览器地址栏复制出来的链接经常没有 `https://`。"""
    return raw if "://" in raw else "https://" + raw.lstrip("/")


def _qq_id_from(raw: str) -> str | None:
    """从 QQ 音乐链接里挖出 songmid。"""
    # `.../songDetail/<mid>` 与 `.../song/<mid>.html`
    for pattern in (
        r"/songDetail/([0-9A-Za-z]{14})(?:[/?#]|$)",
        r"/song/([0-9A-Za-z]{14})(?:\.html)?(?:[/?#]|$)",
    ):
        found = re.search(pattern, raw)
        if found:
            return found.group(1)
    # `?songmid=<mid>` / `&songmid=<mid>`
    found = re.search(r"[?&#]songmid=([0-9A-Za-z]{14})\b", raw)
    if found:
        return found.group(1)
    return None


def _netease_id_from(raw: str) -> str | None:
    """从网易云链接里挖出歌曲 id。

    注意 `#/song?id=123` 里的查询串在 URL 的 **fragment** 里，`urlparse`
    不会把它放进 `query`，所以这里把 path/query/fragment 拼起来再匹配。

    **必须先确认这是「歌曲」页面。** `playlist?id=`、`album?id=`、
    `artist?id=`、`program?id=` 与 `song?id=` 只差一个词；只抓 `id=` 的话，
    贴一个歌单链接会被解析成「歌单里某个 id 对应的歌」，取回完全不相干的歌词，
    而界面上一切正常——这是最坏的失败模式。
    """
    parsed = urlparse(raw)
    tail = f"{parsed.path}?{parsed.query}#{parsed.fragment}"
    if not re.search(r"(?:^|[/#?])song(?:/|\?|#|$)", tail):
        return None
    for pattern in (
        r"[?&#]id=(\d{1,20})\b",
        r"/song/(\d{1,20})(?:[/?#]|$)",
        r"[?&#]ids=(\d{1,20})\b",
    ):
        found = re.search(pattern, tail)
        if found:
            return found.group(1)
    return None


def extract_url(text: str) -> str:
    """从整段分享文本里抠出第一个链接；没有链接时原样返回。"""
    raw = (text or "").strip()
    found = _URL_IN_TEXT.search(raw)
    return found.group(0) if found else raw


def _needs_resolution(url: str) -> bool:
    """这个链接是否**必须**先跟随跳转才能拿到 id。

    注意不能只看路径：`c.y.qq.com/base/fcgi-bin/u?songmid=xxx` 同样是
    `/base/fcgi-bin/u`，但已经把 songmid 带在参数里了，直接解析即可。
    所以调用方是「先尝试直接解析，解析不出来再问这里」。
    """
    host = _host_of(_with_scheme(url))
    if host in _SHORT_LINK_HOSTS:
        return True
    return bool(_host_is(host, _QQ_HOST_SUFFIX) and _QQ_SHARE_PATH.search(url))


def _resolve_or_raise(url: str, resolver):
    """跟随跳转拿到最终地址；拿不到就抛带操作指引的错。"""
    if resolver is None:
        host = _host_of(_with_scheme(url))
        why = _SHORT_LINK_HOSTS.get(host, "这是 App 分享出来的短链接")
        raise UnsupportedLink(
            f"{why}，需要联网跟随跳转才能拿到歌曲 id。"
            "也可以在浏览器里打开它，从地址栏复制跳转后的完整链接"
            "（形如 https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e）。"
        )
    final = resolver(url)
    if not final or final == url:
        raise UnsupportedLink(
            "短链接没有跳转出结果，可能已过期。"
            "请在浏览器里打开它，从地址栏复制完整链接再试。"
        )
    return final


def parse_song_link(text: str, *, resolver=None) -> SongLink:
    """把链接或 `来源:id` 解析成 SongLink；无法识别时抛 UnsupportedLink。

    `resolver` 是一个 `(url) -> 最终 url` 的回调，用来跟随短链跳转。
    **只有直接解析不出 id 时才会调用它**——解析层本身不联网，方便单测。
    """
    raw = extract_url(text)
    if not raw:
        raise UnsupportedLink("请先粘贴歌曲页面链接。" + SUPPORTED_LINK_HELP)

    # 显式写法优先：`qq:xxxx` / `netease:123`（也接受中文冒号）
    explicit = re.match(r"^(qq|netease|netease_cloud|网易云|qq音乐)\s*[:：]\s*(\S+)$",
                        raw, re.IGNORECASE)
    if explicit:
        name = explicit.group(1).lower()
        value = explicit.group(2)
        if name in ("qq", "qq音乐"):
            if not _QQ_MID.match(value):
                raise UnsupportedLink(
                    f"QQ 音乐的歌曲 id 应当是 14 位字母数字，收到 {value!r}。"
                )
            return SongLink("qq", value, raw)
        if not _NUMERIC_ID.match(value):
            raise UnsupportedLink(
                f"网易云的歌曲 id 应当是一串数字，收到 {value!r}。"
            )
        return SongLink("netease", value, raw)

    host = _host_of(_with_scheme(raw))

    if _host_is(host, _QQ_HOST_SUFFIX):
        song_id = _qq_id_from(raw)
        if song_id:
            return SongLink("qq", song_id, raw)
        # 直接解析不出来，而它确实是分享短链 → 跟随跳转再解析一次
        if _needs_resolution(raw):
            return parse_song_link(_resolve_or_raise(raw, resolver))
        raise UnsupportedLink(
            "这是 QQ 音乐的链接，但没有找到歌曲 id。"
            "请打开歌曲详情页后复制地址栏里的完整链接（形如 "
            "https://y.qq.com/n/ryqq/songDetail/002APLOP0C614e）。"
        )

    if _host_is(host, _NETEASE_HOST_SUFFIX):
        song_id = _netease_id_from(raw)
        if song_id:
            return SongLink("netease", song_id, raw)
        raise UnsupportedLink(
            "这是网易云的链接，但没有找到歌曲 id。"
            "请打开歌曲页面后复制地址栏里的完整链接（形如 "
            "https://music.163.com/#/song?id=28341146）。"
        )

    if host in _SHORT_LINK_HOSTS:
        return parse_song_link(_resolve_or_raise(raw, resolver))

    raise UnsupportedLink(
        (f"无法识别的链接（域名 {host or '空'}）。" if host else "无法识别的输入。")
        + SUPPORTED_LINK_HELP
    )
