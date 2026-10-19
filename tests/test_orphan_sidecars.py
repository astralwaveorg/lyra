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
