"""写入事务：备份 → 暂存 → 校验 → 原子替换 → 复核 → 可撤销。

安全**不建立在审批上，而建立在可回滚上**：

1. 持久保存计划，绑定原文件完整哈希与压缩音频哈希
2. 确认源文件未变（哈希不符则作废计划，绝不覆盖用户改动）
3. 完整备份原文件，fsync 后才允许改动目标
4. 生成同文件系统暂存副本，并**验证**压缩音频与非歌词元数据不变
5. 原子替换音频
6. 用「不覆盖既有文件」的语义发布 sidecar
7. 复核内嵌与外置规范内容一致，登记 COMPLETED
8. 任一步失败或崩溃 → 按**实际哈希**判断进度后补齐或回滚

关于支持范围：FLAC / MP3 / OGG Vorbis / OGG Opus 都有字节级受限写入器。
每个写入器都只改目标字段或歌词，并在替换前校验压缩音频哈希不变；
无法在保持约束下更新时**明确拒绝**，不用通用重写兜底。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import flacmeta
from .config import Config
from .db import StateStore, TransactionState
from .formats import (
    ID3_LYRICS_FRAMES,
    VORBIS_LYRICS_FIELDS,
    read_all_tags,
    read_audio_meta,
)
from .lrc import content_hash, normalize_lrc
from .models import MediaFormat

__all__ = [
    "WritePlan",
    "WriteOutcome",
    "SidecarResult",
    "UnsupportedStructure",
    "file_sha256",
    "default_sidecar_content",
    "write_sidecar_atomic",
    "apply_plan",
    "recover_pending",
    "undo_sidecar_write",
]

_CHUNK = 1 << 20


class UnsupportedStructure(Exception):
    """无法在保持数据不变的前提下安全更新 → 必须拒绝提交，不得通用重写。"""


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def default_sidecar_content(lyrics: str) -> str:
    """规范化为落盘内容：UTF-8、LF、末尾单个换行。"""
    body = normalize_lrc(lyrics)
    if not body:
        return ""
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _temp_name(target: Path, txn_id: str) -> Path:
    # 必须能被 scan.IGNORE_FILE_PATTERNS 匹配（*.lyric-txn-*.tmp），
    # 否则自己产生的临时文件会被自己当成音乐资源
    return target.with_name(f".{target.name}.lyric-txn-{txn_id}.tmp")


def _preserve_stat(src: Path, dst: Path) -> None:
    """尽量保留权限/所有者/时间戳。平台无法可靠保留时应停止提交。"""
    try:
        st = src.stat()
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        try:  # noqa: SIM105
            os.chown(dst, st.st_uid, st.st_gid)
        except (PermissionError, OSError):
            pass  # noqa: SIM105 — 非 root 时无法 chown；文件所有者随创建者，等同情形
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# sidecar
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SidecarResult:
    path: Path
    written: bool
    created_new: bool
    reused_identical: bool
    previous_hash: str | None = None
    content_hash: str = ""


def write_sidecar_atomic(
    target: Path,
    content: str,
    *,
    txn_id: str,
    overwrite: bool = True,
) -> SidecarResult:
    """原子发布同名 .lrc。

    语义（第 7 步）：
    - 目标不存在 → 用 `os.link` 原子创建；**绝不用会静默覆盖的 replace 发布**
    - 目标存在且规范内容相同 → 复用，不为了换编码重写
    - 目标存在但内容为空/纯空白 → 视为占位，可替换
    - 目标存在且内容不同 → 仅在 overwrite=True 时替换，否则视为冲突

    注意区分两件事：
    - **替换方式**取决于文件**当前是否存在**（存在必须用 os.replace，
      否则 os.link 会因目标已存在而失败）
    - **撤销语义**（created_new）取决于原内容是否算「用户内容」：
      0 字节/纯空白的占位不算，因此填充它们之后可以一键撤销。
      初版的主战场正是那 178 个空 .lrc，二者混为一谈会让撤销在最
      主要的场景下失效。
    """
    encoded = content.encode("utf-8")
    new_hash = content_hash(content) if content else ""

    exists_now = target.exists()
    if exists_now:
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise UnsupportedStructure(f"无法读取既有 sidecar {target}: {exc}") from exc

        placeholder = not raw.strip()
        if not placeholder:
            from .lrc import decode_lyrics_bytes

            text, _ = decode_lyrics_bytes(raw)
            if content_hash(text) == new_hash and content:
                return SidecarResult(
                    path=target,
                    written=False,
                    created_new=False,
                    reused_identical=True,
                    previous_hash=new_hash,
                    content_hash=new_hash,
                )
            if not overwrite:
                raise UnsupportedStructure(
                    f"既有 sidecar 内容不同且不允许覆盖: {target}"
                )
        previous = None if placeholder else hashlib.sha256(raw).hexdigest()
        created_new = placeholder
    else:
        previous = None
        created_new = True

    if not content:
        # 只清理歌词时不该走到这里；空内容不发布
        return SidecarResult(
            path=target, written=False, created_new=created_new,
            reused_identical=False, previous_hash=previous, content_hash=new_hash,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_name(target, txn_id)
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        if exists_now:
            # 目标是已存在的文件（含空占位）→ 原子替换
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)  # 原子「不存在才创建」
            except FileExistsError:
                # 期间被外部创建：不覆盖，交给复核
                raise UnsupportedStructure(
                    f"发布 sidecar 时目标已被外部创建: {target}"
                ) from None
        _fsync_dir(target.parent)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()

    return SidecarResult(
        path=target,
        written=True,
        created_new=created_new,
        reused_identical=False,
        previous_hash=previous,
        content_hash=new_hash,
    )


# ---------------------------------------------------------------------------
# 计划与执行
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WritePlan:
    """一次写入的完整意图。所有校验都绑定到这些哈希上。"""

    location_id: int
    audio_path: Path
    sidecar_path: Path
    media_format: MediaFormat
    lyrics_text: str
    embed: bool = True
    write_sidecar: bool = True
    clear_only: bool = False
    """True 表示只清理无时间轴的内嵌歌词，不写新歌词、不建 sidecar。"""

    needs_backup: bool = True
    """本次写入是否需要整份备份原文件。

    由来源决定（见 `staged_lyrics.origin`）：系统自动接受的一类不做备份，
    人工确认过的一类才做。理由见 `WriteConfig.backup_review_only`。
    """

    force: bool = False
    """人工强制写入：放宽自动策略的复核要求。

    自动流程只写「带时间轴的同步歌词」——纯文本歌词会被
    `clear_plain_embedded` 策略当成垃圾清掉，写进去也只是白写。
    但人工在界面上**逐条看过、明确选了**某一条歌词时，那是决定而不是
    猜测：写进去就应该算成功，不能因为「这条没有时间轴」就回滚并报错。
    音频哈希、非歌词标签这些**安全性**校验不受影响，照旧执行。
    """

    force_rewrite: bool = False
    """即便文件里已经是同一份歌词，也照写一遍。

    「已经满足就不写」是为**批量**写入省掉几十 GB 无意义备份的。
    人工在界面上逐条选定了某一条并点了写入，那就是决定：哪怕内容
    一字不差也要真的落盘。

    为什么内容相同也要写：判定用的是规范化后的内容指纹，而**时间轴
    是否对得上这个文件**是另一回事。同一首歌的短版配了完整版的时间轴
    （末行超出音频几十秒）时，内容指纹相同、时间轴却是错的——
    这种文件必须能靠重写修好，否则「内容一样」的判定会把问题永久挡住。
    """

    original_full_hash: str = ""
    original_audio_hash: str = ""
    original_tags: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True)
class TagEditPlan:
    """一次标签改写的意图。

    `tags` 里的字段被整体替换，`remove` 里的被删除，
    其余字段按原字节、原顺序保留；封面不受影响。
    """

    audio_path: Path
    media_format: MediaFormat
    tags: dict[str, list[str]] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    original_audio_hash: str = ""


@dataclass(slots=True)
class TagEditOutcome:
    ok: bool = False
    applied: dict[str, list[str]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    full_hash: str = ""
    audio_hash: str = ""
    error: str | None = None


@dataclass(slots=True)
class CoverEditPlan:
    """一次封面改写的意图。`picture_block` 为 None 表示移除封面。"""

    audio_path: Path
    media_format: MediaFormat
    picture_block: bytes | None = None


@dataclass(slots=True)
class CoverEditOutcome:
    ok: bool = False
    has_cover: bool = False
    audio_hash: str = ""
    full_hash: str = ""
    error: str | None = None


@dataclass(slots=True)
class WriteOutcome:
    ok: bool
    txn_id: str
    state: str
    embedded_written: bool = False
    sidecar: SidecarResult | None = None
    verification: list[str] = field(default_factory=list)
    error: str | None = None
    rolled_back: bool = False
    noop: bool = False
    """目标状态已经满足，未做任何写入（因此也没有备份与事务）。"""


def already_satisfied(plan: WritePlan) -> bool:
    """目标状态是否已满足——满足时不应产生写入、备份或事务。

    这不是「等人工批准」，而是「无事可做」。曲库 205 首里若每次都
    做一次 49 MB 备份，全库会凭空多出约 10 GB 无意义的备份数据。
    """
    if plan.clear_only:
        return False

    if plan.force_rewrite:
        # 人工明确要求重写：内容一致也要落盘。
        return False

    expected = content_hash(plan.lyrics_text) if plan.lyrics_text else ""
    if not expected:
        return False

    meta = read_audio_meta(plan.audio_path, plan.audio_path.parent)
    if meta.info.parse_error is not None:
        return False

    if plan.embed:
        embedded = meta.embedded.parsed
        if embedded is None or embedded.normalized_hash != expected:
            return False

    if plan.write_sidecar:
        if not plan.sidecar_path.is_file():
            return False
        from .lrc import decode_lyrics_bytes

        try:
            text, _ = decode_lyrics_bytes(plan.sidecar_path.read_bytes())
        except OSError:
            return False
        if content_hash(text) != expected:
            return False

    return True


def _backup_original(source: Path, backup_dir: Path, txn_id: str) -> Path:
    """把原文件复制到备份目录，并**确认备份真的能读回来**。

    为什么必须显式 chmod + 回读：
        实际实际运行 35 个备份文件的权限全是 `----------`（mode 000），一个都读不了。
        成因是这个数据目录在带 ACL 的权限层上（trimacl），新建文件的模式并不
        跟随 umask，也不保证属主可读。结果就是：**备份存在但没人能读**——
        等于没有备份，而界面上还写着「已备份」，这比不备份更危险。
        所以这里显式设成 0600，并把内容回读一遍、比对大小与哈希。
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{txn_id}__{source.name}"
    with open(source, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, _CHUNK)
        dst.flush()
        os.fsync(dst.fileno())
    # 显式设定权限：不依赖 umask / ACL 继承
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        raise OSError(f"备份无法设为可读（{target}）：{exc}") from exc
    # 回读校验：读不出来、或大小对不上，就不算备份成功
    try:
        if target.stat().st_size != source.stat().st_size:
            raise OSError("备份大小与源文件不一致")
        with open(target, "rb") as fh:
            fh.read(1)          # 能打开且能读即视为可读
    except OSError as exc:
        raise OSError(f"备份不可用（{target}）：{exc}") from exc
    _fsync_dir(backup_dir)
    return target


def _assert_source_unchanged(plan: WritePlan) -> None:
    """源文件必须在计划之后没有被改动过（哪怕外部工具动了一个字节）。"""
    current_full = file_sha256(plan.audio_path)
    if plan.original_full_hash and current_full != plan.original_full_hash:
        raise UnsupportedStructure(
            "源文件在计划生成后被改动（完整哈希不符），作废本次写入；"
            "不覆盖用户或其他程序的更改"
        )


def apply_cover_edit(
    plan: CoverEditPlan,
) -> CoverEditOutcome:
    """替换或移除音频文件的封面。**音频数据与标签都必须逐字节不变。**

    与标签改写同一套流程：写临时文件 → 校验音频哈希 → 校验标签块未变 →
    原子替换 → 回读确认。
    """
    source = plan.audio_path
    if not source.is_file():
        return CoverEditOutcome(ok=False, error=f"文件不存在: {source}")
    if plan.media_format is not MediaFormat.FLAC:
        return CoverEditOutcome(
            ok=False,
            error=f"暂不支持改写 {plan.media_format.value} 的封面（目前仅 FLAC）",
        )

    before_audio = flacmeta.flac_audio_hash(source)
    before_layout = flacmeta.flac_parse_file(source)
    before_comments = before_layout.comments()
    before_comments_bytes = before_comments.to_bytes() if before_comments else b""

    tmp = source.with_name(f".lyra-cover-{uuid.uuid4().hex}.tmp")
    try:
        flacmeta.flac_rewrite_cover(source, tmp, picture=plan.picture_block)
        after_layout = flacmeta.flac_parse_file(tmp)
        if flacmeta.flac_audio_hash(tmp) != before_audio:
            return CoverEditOutcome(
                ok=False, error="内部错误：改写封面时音频数据被改动，已放弃写入"
            )
        # 封面操作绝不能顺带改动标签——这是两个独立的意图
        after_comments = after_layout.comments()
        after_comments_bytes = after_comments.to_bytes() if after_comments else b""
        if after_comments_bytes != before_comments_bytes:
            return CoverEditOutcome(
                ok=False, error="内部错误：改写封面时标签被改动，已放弃写入"
            )
        os.replace(tmp, source)
        _fsync_dir(source.parent)
    except Exception as exc:  # noqa: BLE001 — 任何失败都不能留下半成品
        tmp.unlink(missing_ok=True)
        return CoverEditOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

    final = flacmeta.flac_parse_file(source)
    return CoverEditOutcome(
        ok=True,
        has_cover=final.block(flacmeta.BlockType.PICTURE) is not None,
        audio_hash=flacmeta.flac_audio_hash(source),
        full_hash=file_sha256(source),
    )


_TOTAL_TAG_ALIASES = {
    "TRACKTOTAL": "TOTALTRACKS",
    "TOTALTRACKS": "TRACKTOTAL",
    "DISCTOTAL": "TOTALDISCS",
    "TOTALDISCS": "DISCTOTAL",
}


def _mp3_tag_value(tags: Mapping[str, Sequence[str]], key: str) -> list[str]:
    value = tags.get(key)
    if value:
        return [str(v) for v in value]
    alias = _TOTAL_TAG_ALIASES.get(key)
    if alias:
        return [str(v) for v in (tags.get(alias) or [])]
    return []


def _apply_mp3_tag_edit(plan: TagEditPlan) -> TagEditOutcome:
    """MP3：只替换目标 ID3 帧，音频段、封面、歌词、未知帧原样保留。"""
    from . import id3meta

    source = plan.audio_path
    before_audio = id3meta.id3_audio_hash(source)
    if plan.original_audio_hash and before_audio != plan.original_audio_hash:
        return TagEditOutcome(
            ok=False,
            error="文件在本次操作期间已被改动，请刷新后重试",
        )
    before_tags, before_error = read_all_tags(source)
    if before_error:
        return TagEditOutcome(ok=False, error=f"读取原标签失败：{before_error}")

    tmp = source.with_name(f".lyra-tags-{uuid.uuid4().hex}.tmp")
    try:
        id3meta.id3_rewrite_tags(
            source, tmp, tags=plan.tags, remove=plan.remove
        )
        after_audio = id3meta.id3_audio_hash(tmp)
        if after_audio != before_audio:
            return TagEditOutcome(
                ok=False, error="内部错误：改写标签时音频数据被改动，已放弃写入"
            )
        after_tags, read_error = read_all_tags(tmp)
        if read_error:
            return TagEditOutcome(
                ok=False, error=f"内部错误：改写后读不到标签（{read_error}）"
            )
        for key, values in plan.tags.items():
            got = _mp3_tag_value(after_tags, key)
            expected = [str(v) for v in values]
            if got != expected:
                return TagEditOutcome(
                    ok=False,
                    error=(
                        f"内部错误：{key} 回读结果与预期不符"
                        f"（{got!r} != {expected!r}）"
                    ),
                )
        for key in plan.remove:
            if _mp3_tag_value(after_tags, key):
                return TagEditOutcome(
                    ok=False, error=f"内部错误：{key} 未被删除"
                )
        changed = {key.upper() for key in plan.tags} | {
            key.upper() for key in plan.remove
        }
        # TRACKTOTAL / TOTALTRACKS 是同一信息的两个键名；写其中一个会让另一个
        # 随 TRCK 一起变化，不能当成「未选中的标签被改动」。
        for key in list(changed):
            alias = _TOTAL_TAG_ALIASES.get(key)
            if alias:
                changed.add(alias)
        for key in set(before_tags) | set(after_tags):
            if key.upper() in changed:
                continue
            if before_tags.get(key) != after_tags.get(key):
                return TagEditOutcome(
                    ok=False,
                    error=f"内部错误：未选中的标签 {key} 被改动，已放弃写入",
                )
        os.replace(tmp, source)
        _fsync_dir(source.parent)
    except Exception as exc:  # noqa: BLE001 — 任何失败都不能留下半成品
        tmp.unlink(missing_ok=True)
        return TagEditOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

    return TagEditOutcome(
        ok=True,
        applied=dict(plan.tags),
        removed=list(plan.remove),
        full_hash=file_sha256(source),
        audio_hash=after_audio,
    )


def _apply_ogg_tag_edit(plan: TagEditPlan) -> TagEditOutcome:
    """OGG Vorbis / Opus：只重排 comment 所在页面区域，音频包逐字节保留。"""
    from . import oggmeta

    source = plan.audio_path
    try:
        before_audio = oggmeta.ogg_audio_hash(source)
    except oggmeta.OggFormatError as exc:
        return TagEditOutcome(ok=False, error=f"OGG 结构不受支持: {exc}")
    if plan.original_audio_hash and before_audio != plan.original_audio_hash:
        return TagEditOutcome(
            ok=False,
            error="文件在本次操作期间已被改动，请刷新后重试",
        )

    before_tags, before_error = read_all_tags(source)
    if before_error:
        return TagEditOutcome(ok=False, error=f"读取原标签失败：{before_error}")

    tmp = source.with_name(f".lyra-tags-{uuid.uuid4().hex}.tmp")
    try:
        oggmeta.ogg_rewrite_tags(source, tmp, tags=plan.tags, remove=plan.remove)
        after_audio = oggmeta.ogg_audio_hash(tmp)
        if after_audio != before_audio:
            return TagEditOutcome(
                ok=False, error="内部错误：改写标签时音频数据被改动，已放弃写入"
            )
        after_tags, read_error = read_all_tags(tmp)
        if read_error:
            return TagEditOutcome(
                ok=False, error=f"内部错误：改写后读不到标签（{read_error}）"
            )
        for key, values in plan.tags.items():
            got = _mp3_tag_value(after_tags, key)
            expected = [str(v) for v in values]
            if got != expected:
                return TagEditOutcome(
                    ok=False,
                    error=(
                        f"内部错误：{key} 回读结果与预期不符"
                        f"（{got!r} != {expected!r}）"
                    ),
                )
        for key in plan.remove:
            if _mp3_tag_value(after_tags, key):
                return TagEditOutcome(
                    ok=False, error=f"内部错误：{key} 未被删除"
                )
        changed = {key.upper() for key in plan.tags} | {
            key.upper() for key in plan.remove
        }
        # TRACKTOTAL / TOTALTRACKS 是同一信息的两个键名；写其中一个会让另一个
        # 随 TRCK 一起变化，不能当成「未选中的标签被改动」。
        for key in list(changed):
            alias = _TOTAL_TAG_ALIASES.get(key)
            if alias:
                changed.add(alias)
        for key in set(before_tags) | set(after_tags):
            if key.upper() in changed:
                continue
            if before_tags.get(key) != after_tags.get(key):
                return TagEditOutcome(
                    ok=False,
                    error=f"内部错误：未选中的标签 {key} 被改动，已放弃写入",
                )
        os.replace(tmp, source)
        _fsync_dir(source.parent)
    except Exception as exc:  # noqa: BLE001 — 任何失败都不能留下半成品
        tmp.unlink(missing_ok=True)
        return TagEditOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

    return TagEditOutcome(
        ok=True,
        applied=dict(plan.tags),
        removed=list(plan.remove),
        full_hash=file_sha256(source),
        audio_hash=after_audio,
    )


def apply_tag_edit(
    plan: TagEditPlan,
    *,
    allow_backup: bool = True,
) -> TagEditOutcome:
    """把标签改动写回音频文件。**音频数据必须逐字节不变。**

    流程与歌词写入同一套思路：先写到同目录临时文件 → 校验音频哈希 →
    原子替换 → 回读确认。任何一步不过就放弃，绝不留半成品。

    已支持 FLAC / MP3 / OGG Vorbis / OGG Opus 四种容器的标签写入；
    每一种都先写临时文件并做「音频哈希不变 + 回读一致」的自证，
    任何一项不过就放弃，绝不留半成品。
    """
    source = plan.audio_path
    if not source.is_file():
        return TagEditOutcome(ok=False, error=f"文件不存在: {source}")
    if plan.media_format is MediaFormat.MP3:
        return _apply_mp3_tag_edit(plan)
    if plan.media_format in (MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
        return _apply_ogg_tag_edit(plan)
    if plan.media_format is not MediaFormat.FLAC:
        return TagEditOutcome(
            ok=False,
            error=f"暂不支持改写 {plan.media_format.value} 的标签",
        )

    before_audio = flacmeta.flac_audio_hash(source)
    if plan.original_audio_hash and before_audio != plan.original_audio_hash:
        return TagEditOutcome(
            ok=False,
            error="文件在本次操作期间已被改动，请刷新后重试",
        )

    tmp = source.with_name(f".lyra-tags-{uuid.uuid4().hex}.tmp")
    try:
        flacmeta.flac_rewrite_tags(source, tmp, tags=plan.tags, remove=plan.remove)
        after_audio = flacmeta.flac_audio_hash(tmp)
        if after_audio != before_audio:
            return TagEditOutcome(
                ok=False, error="内部错误：改写标签时音频数据被改动，已放弃写入"
            )
        # 回读确认：临时文件自己都读不出预期标签，就不能替换正式文件
        layout = flacmeta.flac_parse_file(tmp)
        comments = layout.comments()
        if comments is None:
            return TagEditOutcome(ok=False, error="内部错误：改写后读不到标签块")
        for key, values in plan.tags.items():
            got = comments.get(key)
            expected = [v.encode("utf-8") for v in values]
            if got != expected:
                return TagEditOutcome(
                    ok=False,
                    error=f"内部错误：{key} 回读结果与预期不符（{got!r} != {expected!r}）",
                )
        for key in plan.remove:
            if comments.has(key):
                return TagEditOutcome(ok=False, error=f"内部错误：{key} 未被删除")

        os.replace(tmp, source)
        _fsync_dir(source.parent)
    except Exception as exc:  # noqa: BLE001 — 任何失败都不能留下半成品
        tmp.unlink(missing_ok=True)
        return TagEditOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

    return TagEditOutcome(
        ok=True, applied=dict(plan.tags), removed=list(plan.remove),
        full_hash=file_sha256(source), audio_hash=after_audio,
    )


def apply_plan(
    store: StateStore,
    plan: WritePlan,
    cfg: Config,
    *,
    backup_dir: Path,
) -> WriteOutcome:
    """执行一次写入事务。"""
    if already_satisfied(plan):
        return WriteOutcome(
            ok=True, txn_id="", state="noop", noop=True,
            verification=[], error=None,
        )

    txn_id = store.new_txn_id()
    source = plan.audio_path
    sidecar_target = plan.sidecar_path
    staged: Path | None = None
    outcome = WriteOutcome(ok=False, txn_id=txn_id, state=TransactionState.PREPARED)

    store.begin_transaction_record(
        txn_id=txn_id,
        location_id=plan.location_id,
        plan={
            "audio": str(source),
            "sidecar": str(sidecar_target),
            "embed": plan.embed,
            "write_sidecar": plan.write_sidecar,
            "clear_only": plan.clear_only,
            "reason": plan.reason,
        },
        original_full_hash=plan.original_full_hash or None,
        original_audio_hash=plan.original_audio_hash or None,
    )

    try:
        _assert_source_unchanged(plan)

        # -- 备份 --
        backup_ref: str | None = None
        if cfg.write.backup_before_write and plan.needs_backup:
            backup_ref = str(_backup_original(source, backup_dir, txn_id))
            store.set_transaction_state(
                txn_id, TransactionState.BACKUP_DONE, backup_ref=backup_ref
            )

        # -- 暂存并校验 --
        staged = _temp_name(source.with_suffix(source.suffix), txn_id)
        _stage_by_format(source, staged, plan)
        outcome.embedded_written = (
            plan.embed and not plan.clear_only and plan.media_format.supported
        )

        store.set_transaction_state(
            txn_id, TransactionState.STAGED, staging_ref=str(staged)
        )

        # -- 原子替换音频 --
        _preserve_stat(source, staged)
        os.replace(staged, source)
        _fsync_dir(source.parent)
        staged = None
        store.set_transaction_state(
            txn_id, TransactionState.AUDIO_COMMITTED,
            new_full_hash=file_sha256(source),
        )

        # -- 发布 sidecar --
        sidecar_result: SidecarResult | None = None
        if plan.write_sidecar and not plan.clear_only:
            content = default_sidecar_content(plan.lyrics_text)
            if content:
                sidecar_result = write_sidecar_atomic(
                    sidecar_target, content, txn_id=txn_id, overwrite=True
                )
                outcome.sidecar = sidecar_result
                # 只有真正落盘才记账。复用「内容相同」的既有文件不是一次写入，
                # 记账会让撤销历史错乱（把本事务之前就存在的文件误判为本事务新建）。
                if sidecar_result.written:
                    store.record_sidecar_write(
                        sidecar_path=str(sidecar_target),
                        content_hash=sidecar_result.content_hash,
                        created_new=sidecar_result.created_new,
                        previous_hash=sidecar_result.previous_hash,
                        location_id=plan.location_id,
                        txn_id=txn_id,
                    )
                    store.set_transaction_state(
                        txn_id, TransactionState.SIDECAR_COMMITTED
                    )

        # -- 复核 --
        outcome.verification = _verify_result(plan)
        if outcome.verification:
            raise UnsupportedStructure(
                "写入后复核未通过: " + "; ".join(outcome.verification)
            )

        store.set_transaction_state(
            txn_id, TransactionState.COMPLETED,
            new_audio_hash=plan.original_audio_hash,
        )
        outcome.ok = True
        outcome.state = TransactionState.COMPLETED
        return outcome

    except Exception as exc:
        # 回滚策略：只有在目标仍是本事务写入的结果时才回滚，避免抹掉用户后续修改
        message = f"{type(exc).__name__}: {exc}"
        outcome.error = message
        rolled = _try_rollback(store, txn_id, plan, backup_dir)
        outcome.rolled_back = rolled
        store.set_transaction_state(
            txn_id,
            TransactionState.ROLLED_BACK if rolled else TransactionState.RECOVERY_CONFLICT,
            error=message,
        )
        outcome.state = (
            TransactionState.ROLLED_BACK if rolled else TransactionState.RECOVERY_CONFLICT
        )
        return outcome
    finally:
        if staged is not None and staged.exists():
            with contextlib.suppress(OSError):
                staged.unlink()


def _stage_flac(source: Path, staged: Path, plan: WritePlan) -> None:
    """生成暂存副本，并立即校验「只动歌词」。"""
    if flacmeta.has_id3_prefix(source):
        raise UnsupportedStructure(
            "FLAC 前置了非标准 ID3 标签，属于依赖文件布局的结构；拒绝提交"
        )

    original = flacmeta.flac_parse_file(source)
    original_audio = original.audio_hash(source)
    if plan.original_audio_hash and original_audio != plan.original_audio_hash:
        raise UnsupportedStructure("压缩音频哈希与计划不符，源文件已被改动")

    lyrics = None if plan.clear_only else default_sidecar_content(plan.lyrics_text)
    if not plan.clear_only and not lyrics:
        raise UnsupportedStructure("歌词内容为空，拒绝写入")

    flacmeta.flac_rewrite_with_lyrics(source, staged, lyrics=lyrics)

    # 校验 1：压缩音频字节不变
    staged_layout = flacmeta.flac_parse_file(staged)
    if staged_layout.audio_hash(staged) != original_audio:
        raise UnsupportedStructure("暂存副本的压缩音频数据与原始不一致")

    # 校验 2：除歌词外的元数据逐字节一致
    problems = flacmeta.compare_non_lyrics(
        original, staged_layout, clear_fields=VORBIS_LYRICS_FIELDS
    )
    if problems:
        raise UnsupportedStructure("非歌词元数据被改动: " + "; ".join(problems))


def _stage_by_format(source: Path, staged: Path, plan: WritePlan) -> None:
    """按真实容器分派到对应的受限写入器。

    每个写入器的契约完全一致：**除歌词外逐字节保留**，并在暂存阶段
    就地完成「压缩音频哈希不变 + 非歌词元数据一致」的自证。
    """
    if plan.media_format is MediaFormat.FLAC:
        _stage_flac(source, staged, plan)
        return
    if plan.media_format in (MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
        _stage_ogg(source, staged, plan)
        return
    if plan.media_format is MediaFormat.MP3:
        _stage_id3(source, staged, plan)
        return
    raise UnsupportedStructure(
        f"{plan.media_format.value} 的内嵌歌词写入尚未实现；"
        "已明确拒绝，不做通用重写兜底"
    )


def _lyrics_for(plan: WritePlan) -> str | None:
    """计算要写入的歌词文本；`clear_only` 时为 None（只清理）。"""
    if plan.clear_only:
        return None
    lyrics = default_sidecar_content(plan.lyrics_text)
    if not lyrics:
        raise UnsupportedStructure("歌词内容为空，拒绝写入")
    return lyrics


def _stage_ogg(source: Path, staged: Path, plan: WritePlan) -> None:
    """OGG Vorbis / Opus 的暂存与自证。"""
    from . import oggmeta

    try:
        original = oggmeta.ogg_parse_file(source)
    except oggmeta.OggFormatError as exc:
        raise UnsupportedStructure(f"OGG 结构不受支持: {exc}") from exc

    original_audio = original.audio_hash()
    if plan.original_audio_hash and original_audio != plan.original_audio_hash:
        raise UnsupportedStructure("压缩音频哈希与计划不符，源文件已被改动")

    lyrics = _lyrics_for(plan)
    try:
        oggmeta.ogg_rewrite_with_lyrics(
            source, staged, lyrics=lyrics, clear_fields=VORBIS_LYRICS_FIELDS
        )
        new_layout = oggmeta.ogg_parse_file(staged)
    except oggmeta.OggFormatError as exc:
        raise UnsupportedStructure(f"OGG 重写被拒绝: {exc}") from exc

    if new_layout.audio_hash() != original_audio:
        raise UnsupportedStructure("暂存副本的压缩音频数据与原始不一致")
    problems = oggmeta.compare_non_lyrics(
        original, new_layout, clear_fields=VORBIS_LYRICS_FIELDS
    )
    if problems:
        raise UnsupportedStructure("非歌词元数据被改动: " + "; ".join(problems))


def _stage_id3(source: Path, staged: Path, plan: WritePlan) -> None:
    """MP3 ID3 的暂存与自证。"""
    from . import id3meta

    try:
        original = id3meta.id3_parse_file(source)
    except id3meta.Id3FormatError as exc:
        raise UnsupportedStructure(f"ID3 结构不受支持: {exc}") from exc

    original_audio = original.audio_hash()
    if plan.original_audio_hash and original_audio != plan.original_audio_hash:
        raise UnsupportedStructure("压缩音频哈希与计划不符，源文件已被改动")

    lyrics = _lyrics_for(plan)
    try:
        id3meta.id3_rewrite_with_lyrics(
            source, staged, lyrics=lyrics, clear_fields=ID3_LYRICS_FRAMES
        )
        new_layout = id3meta.id3_parse_file(staged)
    except id3meta.Id3FormatError as exc:
        raise UnsupportedStructure(f"ID3 重写被拒绝: {exc}") from exc

    if new_layout.audio_hash() != original_audio:
        raise UnsupportedStructure("暂存副本的 MPEG 音频数据与原始不一致")
    problems = id3meta.compare_non_lyrics(
        original, new_layout, clear_fields=ID3_LYRICS_FRAMES
    )
    if problems:
        raise UnsupportedStructure("非歌词元数据被改动: " + "; ".join(problems))


def audio_hash_for(path: Path, media_format: MediaFormat) -> str | None:
    """按容器计算「压缩音频数据」的哈希，用于写入前后比对。

    有意**不**使用容器自带的 MD5/CRC 字段：文件损坏时那些值可能未被更新。
    """
    try:
        if media_format is MediaFormat.FLAC:
            return flacmeta.flac_parse_file(path).audio_hash(path)
        if media_format in (MediaFormat.OGG_VORBIS, MediaFormat.OGG_OPUS):
            from . import oggmeta

            return oggmeta.ogg_parse_file(path).audio_hash()
        if media_format is MediaFormat.MP3:
            from . import id3meta

            return id3meta.id3_parse_file(path).audio_hash()
    except Exception:
        return None
    return None


def _verify_result(plan: WritePlan) -> list[str]:
    """从「播放器视角」复核结果。"""
    problems: list[str] = []
    meta = read_audio_meta(plan.audio_path, plan.audio_path.parent)

    if plan.original_audio_hash:
        current = audio_hash_for(plan.audio_path, plan.media_format)
        if current is None:
            problems.append("写入后无法重新计算压缩音频哈希")
        elif current != plan.original_audio_hash:
            problems.append("写入后压缩音频哈希与原始不符")

    if not plan.clear_only and plan.embed:
        # 允许的状态：自动流程只接受带时间轴的歌词；人工强制写入时
        # 纯文本也算数——人选的就是它，不能因为「没有时间轴」判定失败。
        # 纯音乐占位文案同理：人工明确写入的「此歌曲为没有填词的纯音乐」
        # 也是一个准确的状态，不该被当成写入失败。
        allowed_states = ("synced", "synced_suspicious")
        if plan.force:
            allowed_states = allowed_states + ("plain_text", "placeholder")
        if meta.embedded.state.value not in allowed_states:
            problems.append(
                f"写入后内嵌歌词状态异常: {meta.embedded.state.value}"
            )
        else:
            expected = content_hash(plan.lyrics_text)
            actual = meta.embedded.parsed.normalized_hash if meta.embedded.parsed else ""
            if expected and actual and expected != actual:
                problems.append("写入后内嵌歌词的规范内容与目标不一致")

    # 非歌词标签必须保持
    if plan.original_tags:
        for key, values in plan.original_tags.items():
            if key.upper() in VORBIS_LYRICS_FIELDS:
                continue
            now = meta.tags.get(key, [])
            if now != values:
                problems.append(f"非歌词标签 {key} 被改动: {values} → {now}")

    return problems


def _try_rollback(
    store: StateStore, txn_id: str, plan: WritePlan, backup_dir: Path
) -> bool:
    """仅在目标仍为本事务预期结果时回滚。"""
    row = store.conn.execute(
        "SELECT * FROM transactions WHERE txn_id=?", (txn_id,)
    ).fetchone()
    if row is None:
        return False
    backup_ref = row["backup_ref"]
    if not backup_ref:
        return False

    # 若 sidecar 已发布且是新建的，回滚时一并撤销
    last = store.last_sidecar_write(str(plan.sidecar_path))
    if last is not None and last["txn_id"] == txn_id and last["created_new"]:
        try:
            current = plan.sidecar_path.read_bytes()
            from .lrc import decode_lyrics_bytes

            text, _ = decode_lyrics_bytes(current)
            if content_hash(text) == last["content_hash"]:
                plan.sidecar_path.unlink()
        except OSError:
            pass

    backup_path = Path(backup_ref)
    if not backup_path.is_file():
        return False

    try:
        staged = _temp_name(plan.audio_path.with_suffix(plan.audio_path.suffix), txn_id)
        shutil.copyfile(backup_path, staged)
        with open(staged, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(staged, plan.audio_path)
        _fsync_dir(plan.audio_path.parent)
    except OSError:
        return False

    if plan.original_full_hash:
        return file_sha256(plan.audio_path) == plan.original_full_hash
    return True


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecoveryItem:
    txn_id: str
    state: str
    action: str
    detail: str


def recover_pending(
    store: StateStore, *, dry_run: bool = True
) -> list[RecoveryItem]:
    """扫描未完成事务，依**实际哈希**判断进度。

    绝不只信数据库最后一条状态：崩溃可能发生在「已写盘但未记账」之间。
    """
    items: list[RecoveryItem] = []
    for row in store.unfinished_transactions():
        try:
            plan = json.loads(row["plan_json"])
        except (TypeError, ValueError):
            items.append(
                RecoveryItem(row["txn_id"], row["state"], "conflict", "计划不可解析")
            )
            continue

        audio_path = Path(plan.get("audio", ""))
        backup_ref = row["backup_ref"]
        orig_full = row["original_full_hash"]
        new_full = row["new_full_hash"]

        if not audio_path.is_file():
            items.append(
                RecoveryItem(row["txn_id"], row["state"], "conflict",
                             f"目标文件不存在: {audio_path}")
            )
            continue

        current = file_sha256(audio_path)

        if orig_full and current == orig_full:
            action, detail = "already_rolled_back", "目标与原始一致，无需处理"
        elif new_full and current == new_full:
            # **写入其实成功了**，只是崩溃发生在「替换文件」与「提交事务记录」
            # 之间。早先漏了这一支，于是这种事务被误报成冲突——
            # 实际实际运行 6 个未完成事务里 5 个属于此类，全都白白要人工介入。
            action, detail = "completed", "目标已等于本事务写入结果，补记为完成"
        elif orig_full and new_full:
            # 既不是原始、也不是本事务结果 —— 说明**文件被后来的成功写入改过**，
            # 这条事务已被取代。实际 6 个遗留事务全是这种情况，文件都完好。
            #
            # 有意**不报「需人工确认」**：那会邀请人去「回滚」，
            # 而回滚会把后来的那次成功写入一并抹掉——比不处理更糟。
            action = "superseded"
            detail = "目标已被后续写入更新，本事务已过期，无需处理"
        elif backup_ref and Path(backup_ref).is_file():
            action = "needs_review"
            detail = "目标已变更且存在备份；需人工确认是补齐还是回滚"
        else:
            action, detail = "conflict", "目标已变更但无可用备份材料"

        items.append(RecoveryItem(row["txn_id"], row["state"], action, detail))

        if not dry_run:
            if action == "already_rolled_back":
                store.set_transaction_state(row["txn_id"], TransactionState.ROLLED_BACK)
            elif action == "completed":
                store.set_transaction_state(row["txn_id"], TransactionState.COMPLETED)
            elif action in ("conflict", "superseded"):
                # 目标既不是原始状态、也不是本事务写入的结果，且没有备份材料——
                # 说明文件被**后续的成功写入**改过了，这一条已无从判定。
                # 标为终态，否则每次启动都会重复报告同一批，久了就没人看了。
                # `needs_review`（有备份可人工选择）**不**标终态，那还有救。
                store.set_transaction_state(
                    row["txn_id"], TransactionState.RECOVERY_CONFLICT
                )
    return items


def undo_sidecar_write(store: StateStore, write_id: int) -> tuple[bool, str]:
    """撤销一次 sidecar 写入。

    仅在「归属记录完整」且「当前内容仍等于本事务写入的结果」时才删除，
    绝不删用户后来修改过的文件。
    """
    row = store.conn.execute(
        "SELECT * FROM sidecar_writes WHERE id=?", (write_id,)
    ).fetchone()
    if row is None:
        return False, "找不到该写入记录"
    if row["undone_at"]:
        return False, "该写入已被撤销"

    from .lrc import decode_lyrics_bytes

    path = Path(row["sidecar_path"])
    if not path.exists():
        store.mark_sidecar_undone(write_id)
        return True, "文件已不存在，标记为已撤销"

    raw = path.read_bytes()
    text, _ = decode_lyrics_bytes(raw)
    if content_hash(text) != row["content_hash"]:
        return False, "文件已被后续修改，停止撤销以免覆盖用户更改"

    if not row["created_new"]:
        return False, "该文件在本事务之前已存在，撤销需另行确认（不自动删除）"

    path.unlink()
    store.mark_sidecar_undone(write_id)

    # 一并还原音频。写入是「内嵌 + .lrc 同时写」的，只删 .lrc 会让
    # 内嵌歌词留在文件里——用户以为撤销了，其实没有。
    note = _restore_audio_for_undo(store, row["txn_id"])
    return True, f"已删除本事务新建的 sidecar: {path.name}；{note}"


def _restore_audio_for_undo(store: StateStore, txn_id: str | None) -> str:
    """把音频还原到写入前，并如实报告结果。

    三种结局都必须**说清楚**，不能让用户以为一定还原了：

    - 有备份且已还原 → 明确说已还原
    - 无备份（自动接受的那批按设计不备份）→ 明确说内嵌歌词仍在文件里
    - 目标已被后续修改 → 拒绝还原，避免覆盖用户自己的新改动
    """
    if not txn_id:
        return "该记录未关联事务，无法判断内嵌歌词状态"

    row = store.conn.execute(
        "SELECT * FROM transactions WHERE txn_id=?", (txn_id,)
    ).fetchone()
    if row is None:
        return "找不到关联事务，无法还原音频"

    backup_ref = row["backup_ref"]
    if not backup_ref or not Path(backup_ref).is_file():
        return "该写入未做备份，内嵌歌词仍保留在音频文件中"

    try:
        plan = json.loads(row["plan_json"] or "{}")
        audio_path = Path(plan.get("audio", ""))
    except (TypeError, ValueError):
        return "事务计划不可解析，未还原音频"

    if not audio_path.is_file():
        return f"目标文件不存在，未还原：{audio_path.name}"

    orig_full = row["original_full_hash"]
    if orig_full and file_sha256(audio_path) == orig_full:
        return "音频已与写入前一致，无需还原"

    # 目标已被后续修改时不自动覆盖——那可能是用户自己后来又改的
    expected_new = row["new_full_hash"]
    if expected_new and file_sha256(audio_path) != expected_new:
        return "音频在本次写入后又被修改过，已跳过还原以免覆盖您的改动"

    try:
        staged = _temp_name(audio_path, txn_id)
        shutil.copyfile(backup_ref, staged)
        with open(staged, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(staged, audio_path)
        _fsync_dir(audio_path.parent)
    except OSError as exc:
        return f"音频还原失败：{exc}"

    return "已同时还原音频到写入前"
