"""库审计的测试。

为什么测这些：

审计是初版的「只读安全入口」，它必须做到两件事，且都只在报告层面发生：

1. **准确分类每首曲目的歌词状态**，尤其是能识别「0 字节 sidecar」这种
   曲库里占 87%（178/205）的真实形态。若 0 字节被判为「已有歌词」，
   这些曲目会被全部拒绝写入，整个项目就失去意义。

2. **检出结构性问题**：孤儿 sidecar、重复 sidecar、命名不一致、容器不符、BOM。

同时这里还有一条不可违反的约束：**审计绝不修改任何文件**。
它是只读入口，一旦它开始写，用户在「只想看看」时就已经被改了东西。
"""

from __future__ import annotations

from pathlib import Path

from lyra.audit import audit_root
from lyra.config import RootConfig
from lyra.models import IssueCode, LyricsState


def _audit(root: Path):
    return audit_root(RootConfig(name="t", path=str(root)))


def _by_rel(result) -> dict[str, object]:
    return {track.audio.rel_path: track for track in result.tracks}


def _issues(track) -> set[IssueCode]:
    return set(track.issues)


# ---------------------------------------------------------------------------
# 0 字节 sidecar：修订 2 的核心
# ---------------------------------------------------------------------------


def test_零字节sidecar被当作不存在(flac_factory, tmp_path: Path):
    """曲库 205 个 .lrc 里有 178 个是 0 字节。

    它们必须被判为 absent / is_effectively_absent，才能被安全填充。
    如果按「已存在文件」处理，工具会声称「已有歌词」而拒绝写入——
    这正是用户抱怨的「歌词不完善」的直接原因。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A"), ("ARTIST", "X")])
    (root / "album" / "01 a.lrc").write_bytes(b"")  # 0 字节

    result = _audit(root)
    track = _by_rel(result)["album/01 a.flac"]

    assert track.sidecar.exists is True, "文件确实存在"
    assert track.sidecar.state is LyricsState.ABSENT, "但状态必须是 absent"
    assert track.sidecar.is_effectively_absent is True, "必须允许被安全填充"
    assert track.effective_state is LyricsState.ABSENT
    assert IssueCode.NO_LYRICS in _issues(track)
    # 同时必须出现在汇总里，便于运维量化「有多少首待填充」
    assert "album/01 a.lrc" in result.empty_sidecars
    assert result.summary.sidecar_empty == 1


def test_纯空白sidecar同样被当作不存在(flac_factory, tmp_path: Path):
    """空白与 0 字节等价——有些工具会写入只有换行的占位文件。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_bytes(b"   \n\n\t\n")

    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert track.sidecar.is_effectively_absent is True
    assert track.effective_state is LyricsState.ABSENT


def test_非空sidecar不会被当作不存在(flac_factory, tmp_path: Path):
    """反向用例：有内容就不能被当成可覆盖的空文件。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_text("[00:01.00]A\n[00:02.00]B\n", encoding="utf-8")

    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert track.sidecar.state is LyricsState.SYNCED
    assert track.sidecar.is_effectively_absent is False
    assert track.effective_state is LyricsState.SYNCED


# ---------------------------------------------------------------------------
# 命名不一致的配对：修过的 bug，务必锁死
# ---------------------------------------------------------------------------


def test_命名不一致但主体相同的lrc被正确配对(flac_factory, tmp_path: Path):
    """`01 B.flac` ↔ `1. B.lrc`：主体都是 "b"，必须配对。

    如果不配对，Lyra 会为同一首歌再新建一个 `01 B.lrc`，产生重复歌词文件；
    同时既有的 `1. B.lrc` 会被误报为孤儿。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 B.flac", entries=[("TITLE", "B"), ("ARTIST", "X")])
    (root / "album" / "1. B.lrc").write_text("[00:01.00]B1\n[00:02.00]B2\n", encoding="utf-8")

    result = _audit(root)
    track = _by_rel(result)["album/01 B.flac"]

    assert track.sidecar.exists is True, "必须复用既有的命名不一致 sidecar"
    assert track.sidecar.state is LyricsState.SYNCED
    assert track.effective_state is LyricsState.SYNCED
    assert IssueCode.SIDECAR_NAME_MISMATCH in _issues(track)
    # 关键：配对成功后不能再报孤儿，否则运维会看到互相矛盾的结论
    assert result.orphan_sidecars == [], f"不应计入孤儿: {result.orphan_sidecars}"


def test_命名不一致时不做重复写入的判定依据(flac_factory, tmp_path: Path):
    """配对后该 lrc 已被「消费」，不应再出现在待处理队列逻辑里。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 B.flac", entries=[("TITLE", "B"), ("ARTIST", "X")])
    (root / "album" / "1. B.lrc").write_text("[00:01.00]A\n[00:02.00]B\n", encoding="utf-8")

    result = _audit(root)
    assert all(
        track.effective_state is not LyricsState.SYNCED for track in result.needs_work
    ) or result.needs_work == []


def test_曲名前缀带歌手时也应能配对(flac_factory, tmp_path: Path):
    """真实库里的异名配对：`09 <歌手> - <曲名>.flac` ↔ `<曲名>.lrc`。

    Lyra 的配对分三级，第三级是「唯一包含匹配」，且要求**双向唯一**：
    该 lrc 只被这一个音频包含、该音频也只被这一个 lrc 包含。
    不做这一级的话，这个 lrc 会被误报为孤儿，Lyra 随后会为同一首歌再建一个
    同名 lrc，产生重复歌词文件。
    """
    root = tmp_path / "lib"
    flac_factory(
        root / "album" / "09 某歌手 - 让你知道.flac", entries=[("TITLE", "让你知道")]
    )
    (root / "album" / "让你知道.lrc").write_text(
        "[00:01.00]A\n[00:02.00]B\n", encoding="utf-8"
    )

    result = _audit(root)
    track = _by_rel(result)["album/09 某歌手 - 让你知道.flac"]
    assert track.sidecar.exists is True, "按曲名命名的 sidecar 应当能配对"
    assert IssueCode.SIDECAR_NAME_MISMATCH in _issues(track), "异名配对必须被标注"
    assert result.orphan_sidecars == [], f"不应误报孤儿: {result.orphan_sidecars}"


def test_包含匹配要求双向唯一(flac_factory, tmp_path: Path):
    """存在第二个可能归属时必须放弃配对，标注歧义。

    这是第三级配对的**关键防御**：`X.lrc` 若同时被两个音频的规范化主体包含，
    说明我们无法断定它属于谁，宁可报歧义也不能张冠李戴。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 某歌手 - 让你知道.flac", entries=[("TITLE", "让你知道")])
    flac_factory(root / "album" / "02 另一歌手 - 让你知道 (Live).flac", entries=[("TITLE", "让你知道")])
    (root / "album" / "让你知道.lrc").write_text("[00:01.00]A\n", encoding="utf-8")

    result = _audit(root)
    tracks = _by_rel(result)
    first = tracks["album/01 某歌手 - 让你知道.flac"]
    assert first.sidecar.exists is False, "双向不唯一时不得绑定候选"
    assert IssueCode.SIDECAR_AMBIGUOUS in _issues(first)
    assert result.orphan_sidecars == ["album/让你知道.lrc"]


def test_精确同名优先于规范化配对(flac_factory, tmp_path: Path):
    """同时存在 `01 C.lrc` 与 `1. C.lrc` 时，必须用精确同名那个，且不报 mismatch。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 C.flac", entries=[("TITLE", "C"), ("ARTIST", "X")])
    (root / "album" / "01 C.lrc").write_text("[00:01.00]精确\n[00:02.00]二\n", encoding="utf-8")
    (root / "album" / "1. C.lrc").write_text("[00:01.00]模糊\n[00:02.00]二\n", encoding="utf-8")

    result = _audit(root)
    track = _by_rel(result)["album/01 C.flac"]

    assert track.sidecar.path is not None
    assert track.sidecar.path.name == "01 C.lrc", "应优先精确同名"
    assert IssueCode.SIDECAR_NAME_MISMATCH not in _issues(track)
    # 另一个没被消费 → 报孤儿；同时两者构成重复组
    assert "album/1. C.lrc" in result.orphan_sidecars


# ---------------------------------------------------------------------------
# 歧义：宁可交人工，不可猜
# ---------------------------------------------------------------------------


def test_同目录多个候选时判为歧义且不配对(flac_factory, tmp_path: Path):
    """`01 D.flac` 面对 `1. D.lrc` 与 `2. D.lrc`：主体都是 "d"，无法断定归属。

    设计约束：出现多个候选时绝不猜。宁可报歧义交人工，
    也不能随便选一个导致歌词张冠李戴。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 D.flac", entries=[("TITLE", "D"), ("ARTIST", "X")])
    (root / "album" / "1. D.lrc").write_text("[00:01.00]候选一\n[00:02.00]二\n", encoding="utf-8")
    (root / "album" / "2. D.lrc").write_text("[00:01.00]候选二\n[00:02.00]二\n", encoding="utf-8")

    result = _audit(root)
    track = _by_rel(result)["album/01 D.flac"]

    assert IssueCode.SIDECAR_AMBIGUOUS in _issues(track)
    assert track.sidecar.exists is False, "歧义时不得绑定任何候选"
    assert track.effective_state is LyricsState.ABSENT
    # 两个候选都未被消费
    assert sorted(result.orphan_sidecars) == ["album/1. D.lrc", "album/2. D.lrc"]


# ---------------------------------------------------------------------------
# 孤儿 / 重复
# ---------------------------------------------------------------------------


def test_孤儿lrc被检出(flac_factory, tmp_path: Path):
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_text("[00:01.00]A\n", encoding="utf-8")
    (root / "album" / "orphan.lrc").write_text("[00:01.00]无主\n", encoding="utf-8")

    result = _audit(root)
    assert result.orphan_sidecars == ["album/orphan.lrc"]
    assert result.summary.sidecar_total == 2


def test_重复lrc被检出(flac_factory, tmp_path: Path):
    """同目录两个 lrc 规范化主体相同 = 疑似同一首歌的重复歌词。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 E.flac", entries=[("TITLE", "E")])
    (root / "album" / "01 E.lrc").write_text("[00:01.00]A\n", encoding="utf-8")
    (root / "album" / "1. E.lrc").write_text("[00:01.00]A\n", encoding="utf-8")

    result = _audit(root)
    assert result.duplicate_sidecars, "必须报出疑似重复"
    directory, group = result.duplicate_sidecars[0]
    assert directory == "album"
    assert group == ["album/01 E.lrc", "album/1. E.lrc"]


# ---------------------------------------------------------------------------
# 容器不符
# ---------------------------------------------------------------------------


def test_容器不符被检出(ogg_factory, tmp_path: Path):
    """OGG 冒充 .flac 必须被标为 CONTAINER_MISMATCH。

    曲库里真有这种文件（蝶恋花.flac / Trophy Boy.flac）。
    它既不能被静默当成 FLAC（会毁文件），也不能不被告知。
    """
    root = tmp_path / "lib"
    ogg_factory(root / "album" / "01 D.flac", entries=[("TITLE", "D")])

    result = _audit(root)
    track = _by_rel(result)["album/01 D.flac"]

    assert track.audio.extension_matches_container is False
    assert IssueCode.CONTAINER_MISMATCH in _issues(track)
    # 容器认出来了，格式不应是 unsupported
    assert track.audio.media_format.supported is True
    assert result.summary.by_format.get("ogg_vorbis") == 1


def test_扩展名与容器一致时不报容器不符(flac_factory, tmp_path: Path):
    """反向用例。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert IssueCode.CONTAINER_MISMATCH not in _issues(track)


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------


def test_带BOM的sidecar被检出(flac_factory, tmp_path: Path):
    """带 BOM 的 LRC 在部分播放器上会显示乱码，必须报告。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_bytes(
        "\ufeff[00:01.00]A\n[00:02.00]B\n".encode("utf-8")
    )

    result = _audit(root)
    assert result.bom_sidecars == ["album/01 a.lrc"]
    # BOM 只是编码问题，歌词本身仍然有效，不能被降级
    track = _by_rel(result)["album/01 a.flac"]
    assert track.sidecar.has_bom is True
    assert track.sidecar.state is LyricsState.SYNCED


def test_无BOM的sidecar不报(flac_factory, tmp_path: Path):
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_text("[00:01.00]A\n", encoding="utf-8")
    assert _audit(root).bom_sidecars == []


# ---------------------------------------------------------------------------
# 内嵌歌词
# ---------------------------------------------------------------------------


def test_内嵌纯文本歌词被标为需清理(flac_factory, tmp_path: Path):
    """无时间轴的内嵌歌词要被清理；审计必须先把它识别出来。"""
    root = tmp_path / "lib"
    flac_factory(
        root / "album" / "01 a.flac",
        entries=[("TITLE", "A"), ("LYRICS", "只有纯文本没有时间轴")],
    )
    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert track.embedded.state is LyricsState.PLAIN_TEXT
    assert IssueCode.PLAIN_EMBEDDED_LYRICS in _issues(track)


def test_内嵌可解析时间轴被认作有效歌词(flac_factory, tmp_path: Path):
    root = tmp_path / "lib"
    flac_factory(
        root / "album" / "01 a.flac",
        entries=[("TITLE", "A"), ("LYRICS", "[00:01.00]A\n[00:02.00]B\n")],
    )
    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert track.embedded.state is LyricsState.SYNCED
    assert track.effective_state is LyricsState.SYNCED


def test_内嵌与sidecar内容不一致时报出(flac_factory, tmp_path: Path):
    """默认「内嵌与 sidecar 两者都要」，两者不一致必须报告而不是假定其一正确。"""
    root = tmp_path / "lib"
    flac_factory(
        root / "album" / "01 a.flac",
        entries=[("TITLE", "A"), ("LYRICS", "[00:01.00]内嵌版本\n[00:02.00]二\n")],
    )
    (root / "album" / "01 a.lrc").write_text(
        "[00:01.00]外置完全不同的歌词\n[00:02.00]二\n", encoding="utf-8"
    )
    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert IssueCode.LYRICS_MISMATCH in _issues(track)


def test_内嵌与sidecar内容一致时不报不一致(flac_factory, tmp_path: Path):
    """反向用例：内容等价（含时间戳写法差异）不应误报。"""
    root = tmp_path / "lib"
    flac_factory(
        root / "album" / "01 a.flac",
        entries=[("TITLE", "A"), ("LYRICS", "[00:01.00]A\n[00:02.00]B\n")],
    )
    (root / "album" / "01 a.lrc").write_text("[00:01.000]A\n[00:02.000]B\n", encoding="utf-8")
    track = _by_rel(_audit(root))["album/01 a.flac"]
    assert IssueCode.LYRICS_MISMATCH not in _issues(track)


# ---------------------------------------------------------------------------
# 只读保证
# ---------------------------------------------------------------------------


def test_审计绝不修改任何文件(flac_factory, ogg_factory, tmp_path: Path, digest_tree):
    """审计必须是纯只读的。

    用户会先「只想看看库的状态」。如果审计顺手改了东西，
    数据保护承诺就从第一步就破产了。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album1" / "01 a.flac", entries=[("TITLE", "A"), ("ARTIST", "X")])
    flac_factory(
        root / "album2" / "01 b.flac",
        entries=[("TITLE", "B"), ("LYRICS", "纯文本歌词")],
    )
    ogg_factory(root / "album3" / "01 c.flac", entries=[("TITLE", "C")])
    (root / "album1" / "01 a.lrc").write_bytes(b"")
    (root / "album2" / "1. b.lrc").write_text("[00:01.00]B\n", encoding="utf-8")
    (root / "album3" / "orphan.lrc").write_text("[00:01.00]无主\n", encoding="utf-8")
    (root / "album1" / ".DS_Store").write_bytes(b"junk")

    before = digest_tree(root)
    first = _audit(root)
    after = digest_tree(root)

    assert before == after, "审计改动了文件"
    # 再跑一次，结果必须稳定（不能有「第一次跑完状态变了」的副作用）
    second = _audit(root)
    assert digest_tree(root) == before
    assert first.summary.tracks_total == second.summary.tracks_total
    assert first.summary.by_lyrics_state == second.summary.by_lyrics_state
    assert first.orphan_sidecars == second.orphan_sidecars


def test_审计不存在的根时不抛异常(tmp_path: Path):
    """挂载故障时不能崩；必须报告为不可读。"""
    missing = tmp_path / "not-there"
    result = _audit(missing)
    assert result.summary.tracks_total == 0
    assert result.unreadable


# ---------------------------------------------------------------------------
# 汇总与 needs_work
# ---------------------------------------------------------------------------


def test_汇总统计与明细一致(flac_factory, tmp_path: Path):
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A"), ("ARTIST", "X")])
    flac_factory(root / "album" / "02 b.flac", entries=[("TITLE", "B"), ("ARTIST", "X")])
    (root / "album" / "02 b.lrc").write_text("[00:01.00]B1\n[00:02.00]B2\n", encoding="utf-8")

    result = _audit(root)
    assert result.summary.tracks_total == 2
    assert result.summary.sidecar_total == 1
    assert sum(result.summary.by_lyrics_state.values()) == 2
    assert result.summary.by_format.get("flac") == 2
    assert result.summary.duration_seconds >= 0


def test_needs_work排除已同步与不可处理(flac_factory, ogg_factory, tmp_path: Path):
    """待处理队列必须排除：已有同步歌词的、以及格式不支持/解析失败的。"""
    root = tmp_path / "lib"
    flac_factory(root / "a" / "01 ok.flac", entries=[("TITLE", "A")])
    (root / "a" / "01 ok.lrc").write_text("[00:01.00]A\n[00:02.00]B\n", encoding="utf-8")
    flac_factory(root / "a" / "02 need.flac", entries=[("TITLE", "B")])
    (root / "a" / "broken.flac").write_bytes(b"not audio at all" * 10)

    result = _audit(root)
    needing = sorted(track.audio.rel_path for track in result.needs_work)
    assert "a/02 need.flac" in needing
    assert "a/01 ok.flac" not in needing, "已有同步歌词的不该再进待处理"
    assert "a/broken.flac" not in needing, "解析失败的不该进待处理"


def test_文本报告可生成(flac_factory, tmp_path: Path):
    """报告是运维唯一的可读出口，不能抛异常。"""
    from lyra.audit import render_audit_text

    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 a.flac", entries=[("TITLE", "A")])
    (root / "album" / "01 a.lrc").write_bytes(b"")
    text = render_audit_text(_audit(root))
    assert "Lyra 库审计报告" in text
    assert "0 字节" in text


# ---------------------------------------------------------------------------
# 纯音乐占位文案：不是异常
# ---------------------------------------------------------------------------


def test_纯音乐占位文案不算时间轴异常(flac_factory, tmp_path: Path):
    """「此歌曲为没有填词的纯音乐」准确描述了这首歌的状态。

    把它算成「时间轴异常」，几十首纯器乐就会在健康页上挂红灯，
    而它们其实什么都不缺——不需要任何人处理。
    """
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 器乐.flac",
                 entries=[("TITLE", "器乐"), ("ARTIST", "X")])
    (root / "album" / "01 器乐.lrc").write_text(
        "[00:00.00]此歌曲为没有填词的纯音乐，请您欣赏\n", encoding="utf-8"
    )

    result = _audit(root)
    track = _by_rel(result)["album/01 器乐.flac"]

    assert track.sidecar.state is LyricsState.PLACEHOLDER
    assert track.effective_state is LyricsState.PLACEHOLDER
    assert IssueCode.SUSPICIOUS_LYRICS not in _issues(track)
    assert IssueCode.NO_LYRICS not in _issues(track)
    assert IssueCode.MISSING_SIDECAR not in _issues(track)


def test_占位与真歌词不一致仍要报出来(flac_factory, tmp_path: Path):
    """一边是占位、一边是真歌词 —— 内容确实对不上，要交人判断。"""
    root = tmp_path / "lib"
    flac_factory(root / "album" / "01 歌.flac",
                 entries=[("TITLE", "歌"), ("ARTIST", "X"),
                          ("LYRICS", "[00:00.00]第一行\n[00:10.00]第二行")])
    (root / "album" / "01 歌.lrc").write_text(
        "[00:00.00]此歌曲为没有填词的纯音乐，请您欣赏\n", encoding="utf-8"
    )

    result = _audit(root)
    track = _by_rel(result)["album/01 歌.flac"]

    assert IssueCode.LYRICS_MISMATCH in _issues(track)
