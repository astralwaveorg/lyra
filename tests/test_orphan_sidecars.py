"""多余歌词（无主 .lrc）的识别与「该做什么」的判定。

## 为什么必须有

`audit_root` 早就统计了 `orphan_sidecars`，但界面上一直没用它——因为**只有
路径的列表无法决定动作**。实际曲库里的两条孤儿都属于同一类：

    `09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac`
        ← [exact] `09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.lrc`
        ← 孤儿    `让你知道.lrc`            （内容逐字节相同）
    `01 漂向北方.flac` ← `01 漂向北方.lrc` ← 孤儿 `1. 漂向北方.lrc`（同样相同）

对这两条，「重新配对改名」是**错的**：改完就会与既有 lrc 同名冲突。
正确动作是清理。而对「音频确实没有歌词」的孤儿，改名配对才是对的。
这个区分必须被测试钉住，否则很容易退化成「统一给一个重新配对按钮」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyra.api import create_app
from lyra.audit import find_orphan_sidecars
from lyra.config import Config, RootConfig, StorageConfig, WebConfig
from lyra.models import RootRole


def _cfg(tmp_path: Path, lib: Path) -> Config:
    return Config(
        roots=[RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))],
        storage=StorageConfig(data_dir=str(tmp_path / "data")),
        web=WebConfig(auth_required=False),
    )


@pytest.fixture
def lib(tmp_path: Path, flac_factory) -> Path:
    lib = tmp_path / "lib"
    # 真实形状一：音频已有一个完全同名的 lrc，另有一份异名的重复歌词
    flac_factory(
        lib / "汪苏泷" / "09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac",
        entries=(("TITLE", "让你知道"), ("ARTIST", "汪苏泷")),
    )
    body = "[00:01.00]词\n[00:02.00]曲\n"
    (lib / "汪苏泷" / "09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.lrc").write_text(
        body, encoding="utf-8")
    (lib / "汪苏泷" / "让你知道.lrc").write_text(body, encoding="utf-8")

    # 形状二：包含关系有歧义（两首音频的曲名都包含它）→ 配对器不会自动配，
    # 只能由人来指定归谁。注意「唯一可推导」的孤儿**不会**留在这里：
    # pair_sidecars 早就会把它们配掉。
    flac_factory(
        lib / "黄明志" / "漂向北方 (Live).flac",
        entries=(("TITLE", "漂向北方 (Live)"), ("ARTIST", "黄明志")),
    )
    flac_factory(
        lib / "黄明志" / "漂向北方 (Remix).flac",
        entries=(("TITLE", "漂向北方 (Remix)"), ("ARTIST", "黄明志")),
    )
    (lib / "黄明志" / "漂向北方.lrc").write_text(
        "[00:04.28]Rap词：那吾克热\n", encoding="utf-8")

    # 正常配对的文件，绝不能出现在孤儿列表里
    flac_factory(
        lib / "正常" / "01 歌.flac",
        entries=(("TITLE", "歌"), ("ARTIST", "某人")),
    )
    (lib / "正常" / "01 歌.lrc").write_text("[00:01.00]正常\n", encoding="utf-8")
    return lib


def test_只列出真正无主的歌词(tmp_path, lib):
    items = find_orphan_sidecars(
        RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
    )
    paths = {i["rel_path"] for i in items}
    assert paths == {"汪苏泷/让你知道.lrc", "黄明志/漂向北方.lrc"}, paths


def test_音频已有歌词时判定为重复并给出可比较的证据(tmp_path, lib):
    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    dup = items["汪苏泷/让你知道.lrc"]
    assert dup["action"] == "duplicate", "音频已经有歌词，这一份是多余的"
    assert dup["guess_audio"] == "汪苏泷/09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.flac"
    assert dup["paired_sidecar"] == "汪苏泷/09 汪苏泷 & G.E.M. 鄧紫棋 - 让你知道.lrc"
    assert dup["same_content"] is True, "内容相同这件事必须能被界面说清"
    assert dup["preview"].startswith("[00:01.00]")


def test_归属有歧义时交给人工指定(tmp_path, lib):
    """两个候选都不能自动配（曲名都只是包含它），必须由人来选，不能瞎猜一个。

    这条固定的正是「为什么不给一个统一的重新配对按钮」：猜错会把歌词挂到
    另一首歌上，而歌词文件一旦错位，播放器显示的就是别的歌的词。
    """
    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    pair = items["黄明志/漂向北方.lrc"]
    assert pair["action"] == "choose", pair
    assert pair["paired_sidecar"] is None
    assert {c["rel_path"] for c in pair["candidates"]} == {
        "黄明志/漂向北方 (Live).flac", "黄明志/漂向北方 (Remix).flac"}


def test_曲库里没有相关音频时只提供清理(tmp_path, lib):
    (lib / "孤儿.lrc").write_text("[00:01.00]谁也不是\n", encoding="utf-8")
    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    lonely = items["孤儿.lrc"]
    assert lonely["action"] == "unmatched"
    assert lonely["candidates"] == []


def test_内容不同时不会声称相同(tmp_path, lib):
    """只有真的逐字节等价才能说「内容重复」，否则人不敢删。"""
    (lib / "汪苏泷" / "让你知道.lrc").write_text(
        "[00:01.00]完全不同的歌词\n", encoding="utf-8")
    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    assert items["汪苏泷/让你知道.lrc"]["same_content"] is False


def test_接口返回分类计数(tmp_path, lib):
    app = create_app(_cfg(tmp_path, lib))
    with TestClient(app) as c:
        r = c.get("/api/v1/orphans")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 2
    assert d["duplicates"] == 1
    assert d["same_content"] == 1
    assert all("root" in i for i in d["items"])


# ---------------------------------------------------------------------------
# 判定「多余」的两条防线（2026-10-28 线上实测后补）
# ---------------------------------------------------------------------------


def test_中间子串不算同一首(tmp_path, flac_factory):
    """`Love Me Do` 与 `ME!` 只是中间子串关系，不能当成同一首。

    线上实测：61 份「无来源 .lrc」里 41 份是 Beatles 的歌，它们所在的专辑
    目录里没有同名音频，模糊匹配把它们挂到了 `1-16 - ME!.flac` 上；那首歌
    恰好有同名 lrc，于是这份真词被判成「重复」并被**默认勾选**——
    点一次「批量删除」就会把 41 份真词删掉。
    """
    lib = tmp_path / "lib"
    flac_factory(
        lib / "Taylor Swift" / "Lover" / "1-16 - ME!.flac",
        entries=(("TITLE", "ME!"), ("ARTIST", "Taylor Swift")),
    )
    (lib / "Taylor Swift" / "Lover").mkdir(parents=True, exist_ok=True)
    (lib / "Taylor Swift" / "Lover" / "1-16 - ME!.lrc").write_text(
        "[00:01.00]ME!\n[00:02.00]oh oh oh\n", encoding="utf-8")
    flac_factory(
        lib / "The Beatles" / "Please Please Me" / "08 - Love Me Do.flac",
        entries=(("TITLE", "Love Me Do"), ("ARTIST", "The Beatles")),
    )
    (lib / "The Beatles" / "Please Please Me" / "08 - Love Me Do.lrc").write_text(
        "[00:01.00]Love me do\n[00:02.00]love me do\n", encoding="utf-8")
    # 孤儿所在目录里没有同名音频（专辑被重组过）
    (lib / "The Beatles" / "1").mkdir(parents=True, exist_ok=True)
    (lib / "The Beatles" / "1" / "01 Love Me Do.lrc").write_text(
        "[00:01.00]Love me do\n[00:02.00]love me do\n", encoding="utf-8")

    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    item = items["The Beatles/1/01 Love Me Do.lrc"]
    assert item["guess_audio"] == (
        "The Beatles/Please Please Me/08 - Love Me Do.flac"
    ), "不能把词挂到 ME! 那首歌上"
    assert item["action"] == "duplicate", "Beatles 这首歌在别处已有歌词"
    me = next(c for c in item["candidates"] if c["rel_path"].endswith("ME!.flac"))
    assert me["how"] == "contained", "中间子串只能当提示，不能参与判定"


def test_候选只有纯音乐占位时不算多余(tmp_path, flac_factory):
    """目标音频那份 lrc 只是「纯音乐」占位 → 这一份可能是它真正的词。

    占位文案不算歌词：那首音频其实还没有词，删掉这一份就没有了。
    """
    lib = tmp_path / "lib"
    flac_factory(
        lib / "James Last" / "05 - She Loves You.flac",
        entries=(("TITLE", "She Loves You"), ("ARTIST", "James Last")),
    )
    (lib / "James Last").mkdir(parents=True, exist_ok=True)
    (lib / "James Last" / "05 - She Loves You.lrc").write_text(
        "[00:01.00]纯音乐，请欣赏\n", encoding="utf-8")
    flac_factory(
        lib / "The Beatles" / "Oldies" / "01 - She Loves You.flac",
        entries=(("TITLE", "She Loves You"), ("ARTIST", "The Beatles")),
    )
    (lib / "The Beatles" / "Oldies" / "01 - She Loves You.lrc").write_text(
        "[00:01.00]She loves you\n[00:02.00]yeah yeah yeah\n", encoding="utf-8")
    (lib / "The Beatles" / "1").mkdir(parents=True, exist_ok=True)
    (lib / "The Beatles" / "1" / "03 She Loves You.lrc").write_text(
        "[00:01.00]She loves you\n[00:02.00]yeah yeah yeah\n", encoding="utf-8")

    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    item = items["The Beatles/1/03 She Loves You.lrc"]
    assert item["action"] == "choose", "有一个候选还没有真歌词，不能判成多余"
    assert item["paired_sidecar"] is None, "占位文案不算「已有歌词」"
    assert item["paired_state"] == "placeholder"
    assert item["guess_audio"] == "James Last/05 - She Loves You.flac", (
        "默认目标应当是那份缺歌词的音频"
    )


def test_内嵌歌词也算那首音频已经有词(tmp_path, flac_factory):
    """内嵌有词、同名 lrc 没有，也算「已有歌词」→ 这一份是多余的。

    只看同名 lrc 会把「内嵌有词」的音频误判成还没有歌词，
    于是真冗余的歌词被留在待处理列表里。
    """
    lib = tmp_path / "lib"
    flac_factory(
        lib / "专辑" / "01 歌.flac",
        entries=(
            ("TITLE", "歌"),
            ("ARTIST", "某人"),
            ("LYRICS", "[00:01.00]内嵌的词\n[00:02.00]还是内嵌的词\n"),
        ),
    )
    (lib / "另一处").mkdir(parents=True, exist_ok=True)
    (lib / "另一处" / "01 歌.lrc").write_text(
        "[00:01.00]外置的词\n[00:02.00]还是外置的词\n", encoding="utf-8")

    items = {
        i["rel_path"]: i
        for i in find_orphan_sidecars(
            RootConfig(name="lib", role=RootRole.LIBRARY, path=str(lib))
        )
    }
    item = items["另一处/01 歌.lrc"]
    assert item["action"] == "duplicate"
    assert item["target_has_lyrics"] is True
