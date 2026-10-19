"""写入事务的测试。

为什么测这些：

写入是**唯一会改动用户音乐资产**的操作，而且这里的风险是**不对称的**：
新建一个 sidecar 不动任何既有字节、撤销就是删文件；覆盖已有内容和改音频才危险。
下面每条断言都对应一个已经实际确认过的安全承诺：

- **幂等不产生副作用。** 目标已满足时必须直接返回 `noop`，**不备份、不写事务记录**。
  曲库 205 首 × 每首约 49 MB 备份 ≈ 10 GB 无用数据；这不是洁癖问题。
- **源文件被外部改动时必须拒绝。** Picard 或其他工具可能在我们算完哈希之后动了文件；
  此时写入会覆盖用户的整理结果。
- **不支持的格式必须明确拒绝**（例如 Ogg FLAC、前置 ID3 的 FLAC），绝不能用通用重写兜底。
- **支持的格式必须真的写对。** FLAC / OGG Vorbis / OGG Opus / MP3 四种容器都要做到
  「压缩音频数据逐字节不变 + 非歌词元数据逐字节保留」。
- **撤销只撤自己新建的、且内容未被改动的文件。** 用户后来的修改永远优先。

注意：**0 字节/纯空白的占位 lrc 不算「本事务之前存在的用户内容」**，
因此填充它们之后可以一键撤销。初版的主战场正是那 178 个空 `.lrc`，
若把它们记成「已存在」，T0 自动写入的可逆性承诺在最主要场景下就是空的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra.config import Config
from lyra.db import TransactionState, open_store
from lyra.flacmeta import flac_audio_hash
from lyra.formats import read_audio_meta
from lyra.models import LyricsState, MediaFormat
from lyra.writer import (
    UnsupportedStructure,
    WritePlan,
    already_satisfied,
    apply_plan,
    audio_hash_for,
    default_sidecar_content,
    file_sha256,
    recover_pending,
    undo_sidecar_write,
    write_sidecar_atomic,
)

LYRICS = "[00:01.00]第一行\n[00:02.00]第二行"
SIDECAR_CONTENT = "[00:01.00]第一行\n[00:02.00]第二行\n"


@pytest.fixture
def harness(tmp_path: Path):
    """一套完整的写入环境：状态库 + 受管根 + 造计划的工厂。"""
    from conftest import make_flac

    store = open_store(tmp_path / "data")
    root = tmp_path / "lib"
    root.mkdir()
    root_id = store.upsert_root(name="main", role="library", path=str(root))
    backup_dir = tmp_path / "data" / "backups"
    cfg = Config()

    class Harness:
        def __init__(self):
            self.store = store
            self.root = root
            self.root_id = root_id
            self.backup_dir = backup_dir
            self.cfg = cfg
            self.make_flac = staticmethod(make_flac)

        def make_track(self, rel_path: str, *, entries=()) -> Path:
            path = root / rel_path
            make_flac(path, entries=entries)
            return path

        def plan(self, path: Path, *, lyrics: str = LYRICS, fmt: MediaFormat | None = None,
                 **overrides) -> WritePlan:
            meta = read_audio_meta(path, root)
            location_id = store.upsert_location(
                root_id=root_id, rel_path=str(path.relative_to(root)),
                size=path.stat().st_size, mtime=path.stat().st_mtime,
            )
            effective = fmt if fmt is not None else meta.info.media_format
            # 文件可能不是合法 FLAC（例如前置了 ID3）；此时没有可用的音频哈希，
            # 留给被测代码按「结构不支持」拒绝。
            try:
                audio_hash = flac_audio_hash(path) if effective is MediaFormat.FLAC else ""
            except Exception:
                audio_hash = ""
            plan = WritePlan(
                location_id=location_id,
                audio_path=path,
                sidecar_path=path.with_suffix(".lrc"),
                media_format=effective,
                lyrics_text=lyrics,
                original_full_hash=file_sha256(path),
                original_audio_hash=audio_hash,
                original_tags=dict(meta.tags),
            )
            for key, value in overrides.items():
                setattr(plan, key, value)
            return plan

        def backups(self) -> list[Path]:
            if not backup_dir.exists():
                return []
            return sorted(p for p in backup_dir.iterdir() if p.is_file())

    helper = Harness()
    try:
        yield helper
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 正常写入
# ---------------------------------------------------------------------------


def test_正常写入完成全部承诺(harness):
    """一次成功的写入必须同时满足：内嵌写入、sidecar 新建、音频不变、非歌词不变。"""
    track = harness.make_track(
        "album/01 song.flac",
        entries=[("TITLE", "某歌"), ("ARTIST", "某歌手"), ("LYRICIST", "某作词人")],
    )
    plan = harness.plan(track)
    audio_before = flac_audio_hash(track)

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)

    assert outcome.ok is True, outcome.error
    assert outcome.noop is False
    assert outcome.state == TransactionState.COMPLETED
    assert outcome.embedded_written is True
    assert outcome.verification == []

    # 音频压缩数据逐字节不变
    assert flac_audio_hash(track) == audio_before
    # 内嵌与 sidecar 都写入了
    assert plan.sidecar_path.is_file()
    assert plan.sidecar_path.read_text(encoding="utf-8") == SIDECAR_CONTENT
    meta = read_audio_meta(track, harness.root)
    assert meta.embedded.state.value in ("synced", "synced_suspicious")
    # 作词人必须原样保留
    assert meta.tags.get("LYRICIST") == ["某作词人"]
    assert meta.tags.get("TITLE") == ["某歌"]


def test_正常写入会留下备份与事务记录(harness):
    """安全建立在可回滚上：备份与事务记录是恢复的唯一依据。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    outcome = apply_plan(
        harness.store, harness.plan(track), harness.cfg, backup_dir=harness.backup_dir
    )

    assert outcome.ok is True
    backups = harness.backups()
    assert len(backups) == 1, "应当恰好备份一份原文件"
    assert harness.store.table_counts()["transactions"] == 1
    row = harness.store.conn.execute(
        "SELECT * FROM transactions WHERE txn_id=?", (outcome.txn_id,)
    ).fetchone()
    assert row["state"] == TransactionState.COMPLETED
    assert row["backup_ref"] == str(backups[0])
    assert row["new_full_hash"] == file_sha256(track)


def test_重复键在写入后逐字节保留(harness):
    """合成文件里放三组重复键，确认写入不动它们。"""
    entries = [
        ("TITLE", "某歌"),
        ("ARTISTS", "甲"),
        ("ARTISTS", "乙"),
        ("ARTISTS", "丙"),
        ("MUSICBRAINZ_ARTISTID", "id-1"),
        ("MUSICBRAINZ_ARTISTID", "id-2"),
    ]
    track = harness.make_track("album/01 song.flac", entries=entries)

    from lyra.flacmeta import flac_parse_file

    before = flac_parse_file(track).comments()
    apply_plan(harness.store, harness.plan(track), harness.cfg, backup_dir=harness.backup_dir)
    after = flac_parse_file(track).comments()

    for key in (b"ARTISTS", b"MUSICBRAINZ_ARTISTID"):
        b = [raw for name, raw in before.entries if name == key]
        a = [raw for name, raw in after.entries if name == key]
        assert b == a, f"{key} 的重复条目被改动"


def test_只清理时不写sidecar(harness):
    """清理无时间轴歌词是独立事务：只删歌词，不产生新文件。"""
    track = harness.make_track(
        "album/01 song.flac",
        entries=[("TITLE", "A"), ("LYRICIST", "某作词人"), ("LYRICS", "纯文本没有时间轴")],
    )
    plan = harness.plan(track, lyrics="", clear_only=True)

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)

    assert outcome.ok is True, outcome.error
    assert outcome.embedded_written is False
    assert not plan.sidecar_path.exists(), "清理事务不应创建 sidecar"
    meta = read_audio_meta(track, harness.root)
    assert meta.embedded.state.value == "absent"
    assert meta.tags.get("LYRICIST") == ["某作词人"], "作词人绝不能被一起清掉"


# ---------------------------------------------------------------------------
# 幂等：不备份、不记事务
# ---------------------------------------------------------------------------


def test_目标已满足时不产生任何副作用(harness):
    """这是防「全库凭空多出约 10 GB 备份」的关键。

    第二次运行时目标状态已满足，必须直接返回 noop：
    **不备份、不产生新事务记录**。如果这里退化成「照常备份再判断」，
    曲库 205 首 × 49 MB 会累积出约 10 GB 无意义备份。
    """
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    first = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    assert first.ok is True

    backups_after_first = len(harness.backups())
    txns_after_first = harness.store.table_counts()["transactions"]

    second_plan = harness.plan(track)
    assert already_satisfied(second_plan) is True

    second = apply_plan(
        harness.store, second_plan, harness.cfg, backup_dir=harness.backup_dir
    )
    assert second.ok is True
    assert second.noop is True
    assert second.txn_id == "", "noop 不应产生事务 id"
    assert second.state == "noop"

    assert len(harness.backups()) == backups_after_first, "noop 不应新增备份"
    assert harness.store.table_counts()["transactions"] == txns_after_first, "noop 不应新增事务记录"
    assert harness.store.table_counts()["sidecar_writes"] == 1, "也不应重复记账"


def test_没有歌词时不视为已满足(harness):
    """反向用例：目标还没写歌词时必须返回 False，否则永远不写。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    assert already_satisfied(harness.plan(track)) is False


def test_只清理永不被视为已满足(harness):
    """clear_only 的完成判定取决于内嵌状态，不能靠 already_satisfied 短路。"""
    track = harness.make_track(
        "album/01 song.flac", entries=[("TITLE", "A"), ("LYRICS", "纯文本")]
    )
    plan = harness.plan(track, lyrics="", clear_only=True)
    assert already_satisfied(plan) is False


# ---------------------------------------------------------------------------
# 外部改动：必须拒绝
# ---------------------------------------------------------------------------


def test_源文件被外部改动后拒绝写入(harness):
    """计划生成后文件被改动（例如 Picard 又跑了一次）→ 作废计划，绝不覆盖。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    plan.original_full_hash = "0" * 64  # 模拟过期哈希

    before = file_sha256(track)
    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)

    assert outcome.ok is False
    assert outcome.error is not None and "被改动" in outcome.error
    assert file_sha256(track) == before, "拒绝时绝不能改动文件"
    assert not plan.sidecar_path.exists(), "拒绝时绝不能产生 sidecar"


def test_压缩音频哈希不符时拒绝(harness):
    """即使完整哈希对得上，压缩音频哈希不符也必须拒绝。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    plan.original_audio_hash = "f" * 64

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    assert outcome.ok is False
    assert "音频哈希" in (outcome.error or "")
    assert not plan.sidecar_path.exists()


def test_前置ID3的FLAC被拒绝(harness):
    """ID3 前置的 FLAC 依赖文件布局，改写会破坏内部偏移，必须拒绝。

    注意两条路径都要守住：
    - 自然探测时文件以 `ID3` 开头会被当作 MP3 处理 → 走「MP3 未实现」的拒绝；
    - 若计划已经按 FLAC 生成（例如分类发生在前），则必须由 `has_id3_prefix`
      这道纵深防御挡住。两种情况下都**不能**去改写文件。
    """
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    track.write_bytes(b"ID3\x03\x00\x00" + track.read_bytes())

    # 路径一：自然探测
    natural = apply_plan(
        harness.store, harness.plan(track), harness.cfg, backup_dir=harness.backup_dir
    )
    assert natural.ok is False, "ID3 前置的文件绝不能被改写"

    # 路径二：强制按 FLAC 处理 → 必须命中 ID3 前置的纵深防御
    forced = harness.plan(track, fmt=MediaFormat.FLAC)
    outcome = apply_plan(harness.store, forced, harness.cfg, backup_dir=harness.backup_dir)
    assert outcome.ok is False
    assert "ID3" in (outcome.error or ""), outcome.error
    assert not forced.sidecar_path.exists()


# ---------------------------------------------------------------------------
# 支持的格式：必须真的写对
# ---------------------------------------------------------------------------


def test_MP3内嵌歌词写入成功且音频不变(harness, real_mp3: Path):
    """MP3 内嵌写入：MPEG 音频数据必须逐字节不变，非歌词帧原样保留。

    这是「只动歌词」在 ID3 上的可验证证明。ID3 的填充区允许变化，
    但 MPEG 数据段与所有非歌词帧不允许有一个字节的差别。
    """
    import shutil

    target = harness.root / "album" / real_mp3.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real_mp3, target)

    plan = harness.plan(target)
    assert plan.media_format is MediaFormat.MP3
    # harness 只对 FLAC 预填音频哈希，这里补上——否则校验会被跳过，
    # 测试就退化成「只断言不抛异常」
    plan.original_audio_hash = audio_hash_for(target, MediaFormat.MP3)
    assert plan.original_audio_hash, "必须能算出 MPEG 音频段哈希"

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)

    assert outcome.ok is True, outcome.error
    assert audio_hash_for(target, MediaFormat.MP3) == plan.original_audio_hash, (
        "MPEG 音频数据被改动"
    )
    assert not outcome.verification, outcome.verification
    assert target.with_suffix(".lrc").exists(), "sidecar 必须一并发布"
    meta = read_audio_meta(target, harness.root)
    assert meta.embedded.state is LyricsState.SYNCED, "内嵌歌词必须可被独立读回"
    for key, values in plan.original_tags.items():
        if key.upper() in {"LYRICS", "UNSYNCEDLYRICS"}:
            continue
        assert meta.tags.get(key) == values, f"非歌词标签 {key} 被改动"


def test_OGG内嵌歌词写入成功且音频不变(harness, ogg_factory):
    """OGG 同理。OGG 的页级重排是允许的（页码与 CRC 必须重算），
    但**音频包数据**与**其余注释项**不允许变化。"""
    target = harness.root / "album" / "song.ogg"
    target.parent.mkdir(parents=True, exist_ok=True)
    ogg_factory(target, entries=[("TITLE", "A"), ("ARTISTS", "甲"), ("ARTISTS", "乙")])

    plan = harness.plan(target)
    assert plan.media_format is MediaFormat.OGG_VORBIS
    plan.original_audio_hash = audio_hash_for(target, MediaFormat.OGG_VORBIS)
    assert plan.original_audio_hash

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)

    assert outcome.ok is True, outcome.error
    assert audio_hash_for(target, MediaFormat.OGG_VORBIS) == plan.original_audio_hash, (
        "OGG 音频包被改动"
    )
    assert not outcome.verification, outcome.verification
    assert target.with_suffix(".lrc").exists()
    meta = read_audio_meta(target, harness.root)
    assert meta.embedded.state is LyricsState.SYNCED
    # 重复键必须原样保留（真实库里 ARTISTS 会出现 3 次）
    assert meta.tags.get("ARTISTS") == ["甲", "乙"], "重复注释项丢失或被合并"


# ---------------------------------------------------------------------------
# sidecar 原子发布
# ---------------------------------------------------------------------------


def test_sidecar新建(tmp_path: Path):
    target = tmp_path / "01 a.lrc"
    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    assert result.written is True
    assert result.created_new is True
    assert target.read_text(encoding="utf-8") == SIDECAR_CONTENT


def test_sidecar内容相同则复用不重写(tmp_path: Path):
    """不为了换编码/写法而重写文件——重写会产生无意义的 mtime 变化与重新扫描。"""
    target = tmp_path / "01 a.lrc"
    write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    before = target.stat().st_mtime_ns

    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t2", overwrite=False)
    assert result.written is False
    assert result.reused_identical is True
    assert target.stat().st_mtime_ns == before


def test_sidecar等价写法也被视为相同(tmp_path: Path):
    """`[00:01.000]` 与 `[00:01.00]` 规范内容一致 → 复用，不重写。"""
    target = tmp_path / "01 a.lrc"
    target.write_text("[00:01.000]第一行\n[00:02.000]第二行\n", encoding="utf-8")
    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    assert result.written is False
    assert result.reused_identical is True


def test_sidecar空文件可被替换(tmp_path: Path):
    """0 字节 sidecar 视为不存在，可以直接填充（修订 2 的写入侧体现）。

    **并且必须记为 created_new=True。** 0 字节文件是占位残留，不是用户的
    内容；若把它记成「本事务之前已存在」，撤销会拒绝删除它——而初版的
    主战场恰好是那 178 个空 `.lrc`，T0 自动写入的可逆性承诺会在最主要的
    场景下失效。替换动作本身仍走 os.replace（因为文件确实存在）。
    """
    target = tmp_path / "01 a.lrc"
    target.write_bytes(b"")
    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    assert result.written is True
    assert result.created_new is True, "空占位文件不算用户内容，必须可一键撤销"
    assert result.previous_hash is None
    assert target.read_text(encoding="utf-8") == SIDECAR_CONTENT


def test_sidecar纯空白文件也可被替换(tmp_path: Path):
    target = tmp_path / "01 a.lrc"
    target.write_bytes(b"  \n\t\n")
    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    assert result.written is True
    assert result.created_new is True, "纯空白同样是占位，不算用户内容"
    assert target.read_text(encoding="utf-8") == SIDECAR_CONTENT


def test_sidecar内容不同且不允许覆盖时抛异常(tmp_path: Path):
    """默认不覆盖任何已有不同内容的 LRC——包括本工具此前生成的。"""
    target = tmp_path / "01 a.lrc"
    target.write_text("[00:09.00]完全不同的歌词\n", encoding="utf-8")

    with pytest.raises(UnsupportedStructure) as excinfo:
        write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    assert "不允许覆盖" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "[00:09.00]完全不同的歌词\n"


def test_sidecar内容不同允许覆盖时替换并记录旧哈希(tmp_path: Path):
    """需人工批准的覆盖路径：替换，但必须留下旧哈希供撤销判定。"""
    target = tmp_path / "01 a.lrc"
    target.write_text("[00:09.00]旧歌词\n", encoding="utf-8")

    result = write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=True)
    assert result.written is True
    assert result.created_new is False
    assert result.previous_hash, "必须记录旧内容哈希"
    assert target.read_text(encoding="utf-8") == SIDECAR_CONTENT


def test_sidecar不留下临时文件(tmp_path: Path):
    """专辑目录里绝不能残留事务临时文件（设计约束）。"""
    target = tmp_path / "01 a.lrc"
    write_sidecar_atomic(target, SIDECAR_CONTENT, txn_id="t1", overwrite=False)
    leftovers = [p.name for p in tmp_path.iterdir() if "lyric-txn" in p.name]
    assert leftovers == [], f"残留临时文件: {leftovers}"


def test_空内容不发布sidecar(tmp_path: Path):
    """清空歌词不应产生一个 0 字节文件。"""
    target = tmp_path / "01 a.lrc"
    result = write_sidecar_atomic(target, "", txn_id="t1", overwrite=False)
    assert result.written is False
    assert not target.exists()


# ---------------------------------------------------------------------------
# 撤销语义
# ---------------------------------------------------------------------------


def test_撤销本事务新建的sidecar(harness):
    """T0 自动写入的可逆性承诺：撤销就是删掉自己新建的文件。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    assert outcome.sidecar is not None and outcome.sidecar.created_new is True

    write_id = harness.store.last_sidecar_write(str(plan.sidecar_path))["id"]
    ok, message = undo_sidecar_write(harness.store, write_id)

    assert ok is True
    assert not plan.sidecar_path.exists(), "本事务新建的文件应被删除"
    assert "已删除" in message


def test_撤销后再次撤销给出明确提示(harness):
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    write_id = harness.store.last_sidecar_write(str(plan.sidecar_path))["id"]

    assert undo_sidecar_write(harness.store, write_id)[0] is True
    ok, message = undo_sidecar_write(harness.store, write_id)
    assert ok is False
    assert "已被撤销" in message


def test_文件被后续修改后拒绝撤销(harness):
    """用户改动永远优先：内容不再等于本事务写入结果时必须停止。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    write_id = harness.store.last_sidecar_write(str(plan.sidecar_path))["id"]

    plan.sidecar_path.write_text("[00:42.00]用户自己改的歌词\n", encoding="utf-8")

    ok, message = undo_sidecar_write(harness.store, write_id)
    assert ok is False
    assert "已被后续修改" in message
    assert plan.sidecar_path.read_text(encoding="utf-8") == "[00:42.00]用户自己改的歌词\n"


def test_本事务之前就存在的文件不自动删除(harness):
    """只有明确记录为「本事务新建」的才允许自动删；否则用户的文件会被误删。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    pre_existing = track.with_suffix(".lrc")
    pre_existing.write_text("[00:09.00]用户原有的歌词\n", encoding="utf-8")

    plan = harness.plan(track)
    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    assert outcome.sidecar is not None
    assert outcome.sidecar.created_new is False

    write_id = harness.store.last_sidecar_write(str(pre_existing))["id"]
    ok, message = undo_sidecar_write(harness.store, write_id)

    assert ok is False
    assert "本事务之前已存在" in message
    assert pre_existing.is_file(), "已有文件不能被自动删除"


def test_文件已不存在时标记为已撤销(harness):
    """用户手动删掉了文件 → 撤销应幂等成功，而不是报错。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    plan = harness.plan(track)
    apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    write_id = harness.store.last_sidecar_write(str(plan.sidecar_path))["id"]

    plan.sidecar_path.unlink()
    ok, message = undo_sidecar_write(harness.store, write_id)
    assert ok is True
    assert "已不存在" in message


def test_撤销不存在的记录给出提示(harness):
    ok, message = undo_sidecar_write(harness.store, 999999)
    assert ok is False
    assert "找不到" in message


def test_复用相同内容不计入撤销历史(harness):
    """复用「内容相同」的既有文件不是一次写入。

    若把它也记账，撤销历史会错乱：会把本事务之前就存在的文件误判为本事务新建，
    进而在撤销时删掉用户的文件。
    """
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    track.with_suffix(".lrc").write_text(SIDECAR_CONTENT, encoding="utf-8")
    plan = harness.plan(track)

    outcome = apply_plan(harness.store, plan, harness.cfg, backup_dir=harness.backup_dir)
    assert outcome.ok is True
    assert outcome.sidecar is not None
    assert outcome.sidecar.reused_identical is True
    assert harness.store.last_sidecar_write(str(plan.sidecar_path)) is None


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------


def test_recover_pending_干跑不修改数据库(harness):
    """dry_run 必须是真只读：运维需要先看清楚再决定。

    有意**直接构造**未完成事务，而不是依赖 `apply_plan` 失败后留下事务——
    后者取决于回滚是否成功，回滚一旦修好测试就会失效（实际发生过）。
    """
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    _interrupted_txn(harness, "dry1", track, state="prepared")

    before = [
        (row["txn_id"], row["state"])
        for row in harness.store.conn.execute("SELECT txn_id, state FROM transactions")
    ]
    assert before, "应当留下未完成事务"

    items = recover_pending(harness.store, dry_run=True)
    assert items, "应当报告未完成事务"

    after = [
        (row["txn_id"], row["state"])
        for row in harness.store.conn.execute("SELECT txn_id, state FROM transactions")
    ]
    assert before == after, "dry_run 不得修改数据库"


def test_recover_pending对已完成事务不报告(harness):
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    apply_plan(harness.store, harness.plan(track), harness.cfg, backup_dir=harness.backup_dir)
    assert recover_pending(harness.store, dry_run=True) == []


def test_recover_pending处理目标缺失(harness):
    """目标是文件不存在 → 报告冲突，不能崩。"""
    track = harness.make_track("album/01 song.flac", entries=[("TITLE", "A")])
    _interrupted_txn(harness, "gone1", track, state="prepared")

    track.unlink()
    items = recover_pending(harness.store, dry_run=True)
    assert items
    assert any(item.action == "conflict" for item in items)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_default_sidecar_content规范化():
    """落盘内容必须与规范化结果一致，否则幂等比较永远不成立。"""
    assert default_sidecar_content("[00:01.000]A\r\n[00:02.00]B") == "[00:01.00]A\n[00:02.00]B\n"
    assert default_sidecar_content("") == ""
    # 逐字标签降级为行级
    assert "<" not in default_sidecar_content("[00:01.00]<00:01.00>A")


def test_file_sha256对内容敏感(tmp_path: Path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    first = file_sha256(path)
    path.write_bytes(b"abd")
    assert file_sha256(path) != first


# ---------------------------------------------------------------------------
# 撤销必须一并还原音频
# ---------------------------------------------------------------------------
#
# 实际问题：写入是「内嵌歌词 + .lrc 同时写」的，但最早的撤销实现**只删 .lrc**，
# 内嵌歌词仍留在音频文件里。用户在界面上点「撤销」，看到 .lrc 消失了，
# 会以为这次写入已经完全回退——**而实际上没有**。
#
# 这里固定三种结局都必须如实报告。


def test_无备份时如实告知内嵌歌词仍在(harness):
    """自动接受的写入按设计不备份，此时**必须明说没还原**。

    如果这种情况也返回「已撤销」，用户就永远不知道文件里还留着歌词。
    """
    from lyra.writer import _restore_audio_for_undo

    # 不存在的 txn → 找不到事务
    assert "无法还原" in _restore_audio_for_undo(harness.store, "不存在的-txn")
    # 没有 txn_id → 明确说无法判断
    assert "无法判断" in _restore_audio_for_undo(harness.store, None)


def test_有备份时还原音频(harness, tmp_path):
    """有备份的情况下，撤销应当把音频恢复到写入前。"""
    from lyra.writer import _restore_audio_for_undo

    audio = tmp_path / "song.flac"
    audio.write_bytes(b"ORIGINAL-AUDIO-CONTENT")
    original_hash = file_sha256(audio)

    # 模拟写入后的状态
    audio.write_bytes(b"MODIFIED-WITH-LYRICS")
    modified_hash = file_sha256(audio)

    backup = tmp_path / "backup.flac"
    backup.write_bytes(b"ORIGINAL-AUDIO-CONTENT")

    import json as _json

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, backup_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        ("t1", loc, _json.dumps({"audio": str(audio)}), "COMMITTED",
         original_hash, modified_hash, str(backup), 0.0, 0.0),
    )
    harness.store.conn.commit()

    note = _restore_audio_for_undo(harness.store, "t1")

    assert "已同时还原" in note, f"应还原音频，实际：{note}"
    assert audio.read_bytes() == b"ORIGINAL-AUDIO-CONTENT"
    assert file_sha256(audio) == original_hash


def test_音频被后续修改时拒绝还原(harness, tmp_path):
    """目标在本次写入后又被改过——那可能是用户自己的改动，不能覆盖。"""
    from lyra.writer import _restore_audio_for_undo

    audio = tmp_path / "song.flac"
    audio.write_bytes(b"USER-CHANGED-AGAIN")

    backup = tmp_path / "backup.flac"
    backup.write_bytes(b"ORIGINAL")

    import json as _json

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, backup_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        ("t2", loc, _json.dumps({"audio": str(audio)}), "COMMITTED",
         "原始哈希", "本事务写入后的哈希", str(backup), 0.0, 0.0),
    )
    harness.store.conn.commit()

    note = _restore_audio_for_undo(harness.store, "t2")

    assert "跳过还原" in note, f"应拒绝覆盖，实际：{note}"
    assert audio.read_bytes() == b"USER-CHANGED-AGAIN", "不能覆盖用户的后续改动"

def _location_id(harness) -> int:
    """取一个可用的 location_id。

    `transactions.location_id` 是 NOT NULL 外键，测试里必须先有位置记录。
    """
    row = harness.store.conn.execute("SELECT id FROM locations LIMIT 1").fetchone()
    if row is not None:
        return int(row[0])
    root_id = harness.store.conn.execute("SELECT id FROM roots LIMIT 1").fetchone()
    if root_id is None:
        root_id = harness.store.upsert_root(name="t", role="library", path="/tmp")
        root_id = (root_id,)
    return int(harness.store.upsert_location(
        root_id=int(root_id[0]), rel_path="t.flac", size=1, mtime=1.0
    ))


def test_撤销走完整流程时音频真的被还原(harness):
    """**集成测试**：走完整的 `undo_sidecar_write`，验证音频哈希回到写入前。

    ## 为什么必须有这一条

    上面几条只测了 `_restore_audio_for_undo` 函数本身。
    实际发现：把 `undo_sidecar_write` 里调用它的那一行删掉，
    **那些测试依然全部通过**——因为它们没有覆盖调用点。

    这正是「测试查错对象 = 没有测试」。所以这里必须从入口走一遍。
    """
    from lyra.writer import file_sha256

    # 让所有写入都备份，否则自动接受的写入按设计不备份，无从还原
    harness.cfg.write.backup_review_only = False

    track = harness.make_track("album/undo.flac", entries=[("TITLE", "A")])
    before = file_sha256(track)

    plan = harness.plan(track)
    outcome = apply_plan(
        harness.store, plan, harness.cfg, backup_dir=harness.backup_dir
    )
    assert outcome.ok, f"写入应成功：{outcome.error}"

    after = file_sha256(track)
    assert after != before, "写入后音频哈希应当变化（内嵌了歌词）"

    write_id = harness.store.last_sidecar_write(str(plan.sidecar_path))["id"]
    ok, message = undo_sidecar_write(harness.store, write_id)

    assert ok is True, message
    assert file_sha256(track) == before, (
        f"撤销后音频应回到写入前，实际：{message}"
    )
    assert "已同时还原" in message, f"应报告已还原音频，实际：{message}"


def test_恢复识别写入其实已成功的事务(harness, tmp_path):
    """崩溃发生在「替换文件」与「提交事务记录」之间时，**写入是成功的**。

    早先的恢复逻辑只判断两种情况：目标等于原始（已回滚）、有备份（需确认）。
    **漏了「目标等于本事务写入结果」这一支**，于是这种事务被误报成冲突。

    实际实际运行 6 个未完成事务里 5 个属于此类——它们本来无需人工介入。
    """
    import json as _json

    from lyra.writer import file_sha256, recover_pending

    audio = tmp_path / "song.flac"
    audio.write_bytes(b"WRITTEN-WITH-LYRICS")
    new_hash = file_sha256(audio)

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("ok1", loc, _json.dumps({"audio": str(audio)}), "prepared",
         "写入前的哈希", new_hash, 0.0, 0.0),
    )
    harness.store.conn.commit()

    items = recover_pending(harness.store, dry_run=True)
    mine = [i for i in items if i.txn_id == "ok1"]
    assert mine, "该事务应当被恢复流程看到"
    assert mine[0].action == "completed", (
        f"目标已等于写入结果，应判为已完成，实际：{mine[0].action} — {mine[0].detail}"
    )


def test_恢复把已成功的事务补记为完成(harness, tmp_path):
    """非干跑时应当真的把状态改成终态，否则每次启动都会重复报告。"""
    import json as _json

    from lyra.writer import file_sha256, recover_pending

    audio = tmp_path / "song2.flac"
    audio.write_bytes(b"WRITTEN")
    new_hash = file_sha256(audio)

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("ok2", loc, _json.dumps({"audio": str(audio)}), "prepared",
         "旧哈希", new_hash, 0.0, 0.0),
    )
    harness.store.conn.commit()

    recover_pending(harness.store, dry_run=False)

    state = harness.store.conn.execute(
        "SELECT state FROM transactions WHERE txn_id='ok2'"
    ).fetchone()["state"]
    assert state in ("COMMITTED", "completed"), f"应补记为完成，实际：{state}"

    # 再跑一次不应重复报告
    again = [i for i in recover_pending(harness.store, dry_run=True) if i.txn_id == "ok2"]
    assert not again, "已完成的事务不该再出现在恢复报告里"


def test_记录不完整的事务标为终态(harness, tmp_path):
    """缺少哈希信息时无从判定，必须**标为终态**。

    否则每次启动都重复报告同一批——报告久了没人看，等于没有报告。

    （哈希齐全时走 `superseded` 分支，见上一条测试：
    那种情况能确定是被后续写入取代。）
    """
    import json as _json

    from lyra.writer import recover_pending

    audio = tmp_path / "song3.flac"
    audio.write_bytes(b"SOMETHING-ELSE-ENTIRELY")

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        # 有意不给 new_full_hash —— 记录不完整，无法判定是否被取代
        ("c1", loc, _json.dumps({"audio": str(audio)}), "prepared",
         "原始哈希", None, 0.0, 0.0),
    )
    harness.store.conn.commit()

    items = recover_pending(harness.store, dry_run=True)
    mine = [i for i in items if i.txn_id == "c1"]
    assert mine and mine[0].action == "conflict"

    recover_pending(harness.store, dry_run=False)

    state = harness.store.conn.execute(
        "SELECT state FROM transactions WHERE txn_id='c1'"
    ).fetchone()["state"]
    assert state == "recovery_conflict", f"应标为终态，实际：{state}"

    again = [i for i in recover_pending(harness.store, dry_run=True) if i.txn_id == "c1"]
    assert not again, "已标终态的事务不该再次出现在恢复报告里"

def _interrupted_txn(harness, txn_id: str, audio: Path, *, state: str = "prepared") -> None:
    """直接构造一条未完成事务记录。

    比「让 apply_plan 失败」更确定：不依赖回滚行为。
    """
    import json as _json

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (txn_id, loc, _json.dumps({"audio": str(audio)}), state,
         "原始哈希", "写入后哈希", 0.0, 0.0),
    )
    harness.store.conn.commit()


def test_被后续写入取代的事务不再邀请回滚(harness, tmp_path):
    """目标既非原始、也非本事务结果 → 文件被**后来的成功写入**改过。

    这种情况**不能报「需人工确认」**：那会邀请人去「回滚」，
    而回滚会把后来的那次成功写入一并抹掉——比不处理更糟。

    实际实际运行 6 个遗留事务全是这种情况，文件实际都完好。
    """
    import json as _json

    from lyra.writer import recover_pending

    audio = tmp_path / "song4.flac"
    audio.write_bytes(b"LATER-SUCCESSFUL-WRITE")
    backup = tmp_path / "backup.flac"
    backup.write_bytes(b"OLD")

    loc = _location_id(harness)
    harness.store.conn.execute(
        "INSERT INTO transactions(txn_id, location_id, plan_json, state,"
        " original_full_hash, new_full_hash, backup_ref, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        ("s1", loc, _json.dumps({"audio": str(audio)}), "backup_done",
         "更早的原始哈希", "本事务结果哈希", str(backup), 0.0, 0.0),
    )
    harness.store.conn.commit()

    items = recover_pending(harness.store, dry_run=True)
    mine = [i for i in items if i.txn_id == "s1"]
    assert mine, "该事务应被看到"
    assert mine[0].action == "superseded", (
        f"被后续写入取代的事务不该报需人工确认，实际：{mine[0].action}"
    )

    recover_pending(harness.store, dry_run=False)
    state = harness.store.conn.execute(
        "SELECT state FROM transactions WHERE txn_id='s1'"
    ).fetchone()["state"]
    assert state == "recovery_conflict", f"应标为终态，实际：{state}"


def test_备份文件必须可读且内容一致(tmp_path, flac_factory):
    """备份不可读 = 没有备份，而界面上还写着「已备份」。

    实际实际运行 35 个备份的权限全是 `----------`（mode 000）：数据目录在带 ACL
    的权限层上（trimacl），新建文件的模式不跟随 umask。这里固定两条：
    备份的权限必须是属主可读，且回读出来的字节与源文件一致。
    """
    import stat as stat_module

    from lyra.writer import _backup_original

    src = flac_factory(tmp_path / "lib" / "歌.flac", entries=(("TITLE", "歌"),))
    payload = src.read_bytes()
    backup_dir = tmp_path / "data" / "backups"

    target = _backup_original(src, backup_dir, "txn123")

    mode = stat_module.S_IMODE(target.stat().st_mode)
    assert mode & stat_module.S_IRUSR, f"备份属主不可读（mode {oct(mode)}）"
    assert target.read_bytes() == payload, "备份内容与源文件不一致"
    assert target.name == "txn123__歌.flac"
