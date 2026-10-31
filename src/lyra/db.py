"""SQLite 状态库：schema、迁移与仓储。

约束：

- 状态必须**互相分离**：歌词观察 / 排除规则 / 来源解析 / 任务 / 事务
  各自独立成表，不能合并成一个「大状态」。
- 身份策略：**路径表示位置，音频哈希表示本地录音数据，MusicBrainz ID 是匹配证据**，
  三者不能混为一个主键。
- 幂等依据内容与持久状态，**不能只依赖 mtime**。
- 数据库 schema 版本化；迁移前先备份；失败则拒绝启动写入。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "StateStore",
    "DatabaseError",
    "JobState",
    "ResolutionOutcome",
    "TransactionState",
]

SCHEMA_VERSION = 20


class DatabaseError(Exception):
    pass


class JobState(str):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    PARKED = "parked"
    NEEDS_REVIEW = "needs_review"


class ResolutionOutcome(str):
    """来源解析结果。

    这些状态**必须区分开**（见架构文档）。
    把技术失败当成「没有歌词」，是最危险的错误。
    """

    MATCHED = "matched"
    NOT_FOUND = "not_found"
    NO_SYNCED_LYRICS = "no_synced_lyrics"
    NEEDS_REVIEW = "needs_review"
    SEARCH_INCOMPLETE = "search_incomplete"
    TRANSIENT_FAILED = "transient_failed"
    SCHEMA_FAILED = "schema_failed"
    AUTH_REQUIRED = "auth_required"


class TransactionState(str):
    PREPARED = "prepared"
    BACKUP_DONE = "backup_done"
    STAGED = "staged"
    AUDIO_COMMITTED = "audio_committed"
    SIDECAR_COMMITTED = "sidecar_committed"
    COMPLETED = "completed"
    RECOVERY_CONFLICT = "recovery_conflict"
    ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_MIGRATION_1 = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 受管根
CREATE TABLE roots (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    role               TEXT NOT NULL,
    path               TEXT NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    read_only          INTEGER NOT NULL DEFAULT 0,
    health             TEXT NOT NULL DEFAULT 'unknown',
    scan_generation    INTEGER NOT NULL DEFAULT 0,
    last_full_scan_at  REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

-- 音频资产：以「压缩音频数据哈希」为身份，路径不参与
CREATE TABLE assets (
    id            INTEGER PRIMARY KEY,
    audio_hash    TEXT NOT NULL,
    media_format  TEXT NOT NULL,
    duration      REAL,
    created_at    REAL NOT NULL,
    UNIQUE (audio_hash, media_format)
);

-- 位置：路径表示「在哪里」，可与 asset 多对一（同一录音的多个位置）
CREATE TABLE locations (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path       TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    mtime          REAL NOT NULL DEFAULT 0,
    ctime          REAL,
    inode          INTEGER,
    dir_identity   TEXT,
    full_hash      TEXT,
    asset_id       INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    last_seen_at   REAL NOT NULL,
    missing_since  REAL,
    UNIQUE (root_id, rel_path)
);
CREATE INDEX idx_locations_asset ON locations(asset_id);
CREATE INDEX idx_locations_root ON locations(root_id, missing_since);

-- 匹配用的元数据版本
CREATE TABLE metadata_revisions (
    id                          INTEGER PRIMARY KEY,
    location_id                 INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    title                       TEXT,
    artists                     TEXT,
    album                       TEXT,
    albumartist                 TEXT,
    track_number                TEXT,
    disc_number                 TEXT,
    date                        TEXT,
    isrc                        TEXT,
    musicbrainz_trackid         TEXT,
    musicbrainz_releasetrackid  TEXT,
    musicbrainz_albumid         TEXT,
    -- 规范化值仅用于「比较」，不改原始文件
    normalized_title            TEXT,
    normalized_artists          TEXT,
    revision_hash               TEXT NOT NULL,
    created_at                  REAL NOT NULL
);
CREATE INDEX idx_meta_loc ON metadata_revisions(location_id, created_at DESC);

-- 歌词观察（读到的状态，不代表内容正确）
CREATE TABLE lyric_observations (
    id                  INTEGER PRIMARY KEY,
    location_id         INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    embedded_state      TEXT NOT NULL,
    embedded_fields     TEXT,
    embedded_hash       TEXT,
    sidecar_state       TEXT,
    sidecar_hash        TEXT,
    sidecar_size        INTEGER,
    sidecar_path        TEXT,
    sidecar_names_differ INTEGER NOT NULL DEFAULT 0,
    has_embedded_cover  INTEGER NOT NULL DEFAULT 0,
    issues              TEXT,
    observed_at         REAL NOT NULL
);
CREATE INDEX idx_lyric_obs_loc ON lyric_observations(location_id, observed_at DESC);

-- 用户排除 / 纳入规则
CREATE TABLE rules (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,   -- global | root | path | asset
    target      TEXT NOT NULL,
    action      TEXT NOT NULL,   -- skip_all | skip_fetch | include
    reason      TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    UNIQUE (scope, target, action)
);

-- 任务队列
CREATE TABLE jobs (
    id                INTEGER PRIMARY KEY,
    job_key           TEXT NOT NULL UNIQUE,
    job_type          TEXT NOT NULL,
    root_id           INTEGER REFERENCES roots(id) ON DELETE CASCADE,
    location_id       INTEGER REFERENCES locations(id) ON DELETE CASCADE,
    state             TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    lease_owner       TEXT,
    lease_expires_at  REAL,
    next_run_at       REAL,
    last_error_class  TEXT,
    last_error        TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX idx_jobs_due ON jobs(state, next_run_at);
CREATE INDEX idx_jobs_loc ON jobs(location_id);

-- 来源解析结果（含持久负缓存）
CREATE TABLE resolutions (
    id              INTEGER PRIMARY KEY,
    resolution_key  TEXT NOT NULL UNIQUE,
    asset_id        INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    provider_id     TEXT,
    candidate_json  TEXT,
    lyrics_hash     TEXT,
    query_log       TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX idx_res_retry ON resolutions(outcome, next_retry_at);

-- 批准记录（自动模式也记录来源，便于审计）
CREATE TABLE approvals (
    id                   INTEGER PRIMARY KEY,
    location_id          INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    asset_id             INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    lyrics_hash          TEXT NOT NULL,
    metadata_revision_id INTEGER REFERENCES metadata_revisions(id) ON DELETE SET NULL,
    approved_by          TEXT NOT NULL,   -- auto | user
    policy_version       TEXT,
    approved_at          REAL NOT NULL,
    revoked_at           REAL
);
CREATE INDEX idx_approvals_loc ON approvals(location_id, approved_at DESC);

-- 写入事务
CREATE TABLE transactions (
    id                  INTEGER PRIMARY KEY,
    txn_id              TEXT NOT NULL UNIQUE,
    location_id         INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    plan_json           TEXT NOT NULL,
    state               TEXT NOT NULL,
    original_full_hash  TEXT,
    new_full_hash       TEXT,
    original_audio_hash TEXT,
    new_audio_hash      TEXT,
    backup_ref          TEXT,
    staging_ref         TEXT,
    error               TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX idx_txn_state ON transactions(state, updated_at);

-- sidecar 写入记录：支撑「一键撤销」与「不覆盖用户文件」判定
CREATE TABLE sidecar_writes (
    id             INTEGER PRIMARY KEY,
    txn_id         TEXT,
    location_id    INTEGER REFERENCES locations(id) ON DELETE CASCADE,
    sidecar_path   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    created_new    INTEGER NOT NULL DEFAULT 0,
    previous_hash  TEXT,
    written_at     REAL NOT NULL,
    undone_at      REAL
);
CREATE INDEX idx_sidecar_loc ON sidecar_writes(location_id, written_at DESC);
"""

_MIGRATION_2 = """
-- 已抓取但尚未落盘的歌词。
-- 「抓取」与「写入」必须分离：抓取可以放心跑，
-- 写入才需要备份与事务。这里保存抓取结果，apply 阶段按内容哈希取回，
-- 因此 apply 与 fetch 之间即便隔了重启也不会错配内容。
CREATE TABLE staged_lyrics (
    id              INTEGER PRIMARY KEY,
    location_id     INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    content_hash    TEXT NOT NULL,
    content         TEXT NOT NULL,
    provider        TEXT NOT NULL,
    provider_id     TEXT,
    score           REAL,
    duration_delta  REAL,
    state           TEXT NOT NULL DEFAULT 'staged',
    created_at      REAL NOT NULL,
    UNIQUE (location_id, content_hash)
);
CREATE INDEX idx_staged_loc ON staged_lyrics(location_id, created_at DESC);
CREATE INDEX idx_staged_state ON staged_lyrics(state, created_at);
"""

_MIGRATION_3 = """
-- 修正性重命名记录（当前用途：扩展名与真实容器不符）。
-- 这是**路径变更**，属于风险最高的一类操作，因此必须：
--   1. 先验证真实容器，绝不凭扩展名或猜测改名
--   2. 记录原路径，支持一键撤销
--   3. 与「按刮削结果重命名」严格区分——后者本工具永不做
CREATE TABLE renames (
    id             INTEGER PRIMARY KEY,
    location_id    INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    root_id        INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    old_rel_path   TEXT NOT NULL,
    new_rel_path   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    media_format   TEXT NOT NULL,
    detail         TEXT,
    renamed_at     REAL NOT NULL,
    undone_at      REAL
);
CREATE INDEX idx_renames_loc ON renames(location_id, renamed_at DESC);
CREATE INDEX idx_renames_old ON renames(root_id, old_rel_path);
"""

_MIGRATION_4 = """
-- 鉴权。
-- 只存 scrypt 哈希，绝不存明文；首次启动生成随机密码。
-- Token 也只存哈希：数据库泄露不应等价于凭据泄露。
CREATE TABLE auth_users (
    id             INTEGER PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE auth_tokens (
    id            INTEGER PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,
    label         TEXT,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    last_used_at  REAL,
    revoked_at    REAL
);
CREATE INDEX idx_tokens_hash ON auth_tokens(token_hash);
"""

_MIGRATION_5 = """
-- 记录该条暂存歌词是怎么来的，写入时据此决定要不要整份备份。
--   auto   = 系统自动接受（时长吻合 + 分数达标 + 标题艺人达标）
--   review = 人工在复核队列点了「接受」
--   manual = 人工在来源对比里选了某个来源
-- 自动接受的一类写错概率低，而为每个文件整份复制代价极高
-- （实际单文件 24~142MB，全库十几 GB），所以默认不给它们做备份。
ALTER TABLE staged_lyrics ADD COLUMN origin TEXT NOT NULL DEFAULT 'auto';
"""

_MIGRATION_6 = """
-- 后台任务的状态落库。原来只存在内存里，重启容器就消失——
-- 实际因此丢失过三次正在运行的抓取/写入任务，而且用户看不到任何痕迹。
-- 落库之后：任务历史可查，被重启打断的任务会被明确标为 interrupted，
-- 而不是静默消失。
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    progress    TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    error       TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX idx_tasks_started ON tasks(started_at DESC);
"""

_MIGRATION_7 = """
-- 跨进程写租约。Web 容器里的批量任务与独立的监控容器都会全库写入，
-- 两者同时跑会重复请求上游、互相争 SQLite 写锁，而且每次都把整库跑一遍。
-- 有了租约，监控发现批量任务在跑就跳过本轮，等它结束再继续。
-- 用 expires_at 而不是靠释放：进程被杀时锁必须能自动过期，否则会永久卡住。
CREATE TABLE write_locks (
    name       TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

_MIGRATION_8 = """
-- 把解析记录归属到具体曲目。
-- 原来只有 asset_id，而 pipeline 从没给它赋过值（一直是 NULL），
-- 于是「这一首为什么没匹配上」根本查不出来——只能看全局统计。
-- location_id 才是真实存在的外键。
ALTER TABLE resolutions ADD COLUMN location_id INTEGER REFERENCES locations(id) ON DELETE CASCADE;
CREATE INDEX idx_res_location ON resolutions(location_id);
"""

_MIGRATION_9 = """
-- 监控进程的活动历史。
-- 原来只有 meta 里的「最后一次心跳」，看不到「过去一天它做了什么」——
-- 而监控是自动抓取与写入的唯一执行者，它的历史对判断「是否正常」至关重要。
CREATE TABLE watch_log (
    id            INTEGER PRIMARY KEY,
    at            REAL NOT NULL,
    stage         TEXT NOT NULL,
    detail        TEXT,
    counters_json TEXT
);
CREATE INDEX idx_watch_log_at ON watch_log(at DESC);
"""

_MIGRATION_10 = """
-- 人工写入请求。监控进程持有租约时，人工操作不该干等或直接失败。
-- 人工请求先登记一行，监控在每写完一个文件后检查：有人在等就立刻收手、
-- 释放租约。这样「人工优先」是协议的一部分，而不是靠缩短批处理时间来碰运气。
CREATE TABLE write_waits (
    owner        TEXT PRIMARY KEY,
    requested_at REAL NOT NULL
);
"""

_MIGRATION_11 = """
-- 标签刮削的待复核提案。每个位置只保留**一条当前提案**：
-- 重新扫描会覆盖它，采用/跳过之后由状态决定是否还展示。
-- 提案体（本地值、来源值、逐字段动作、来源说明）整体存 JSON——
-- 字段集合由 MusicBrainz 决定，会随来源演进，用固定列会反复迁移。
CREATE TABLE tag_proposals (
    id             INTEGER PRIMARY KEY,
    location_id    INTEGER NOT NULL UNIQUE REFERENCES locations(id) ON DELETE CASCADE,
    state          TEXT NOT NULL,              -- auto | review | unmatched | ignored | done
    source         TEXT NOT NULL,              -- mbid | album | search | fingerprint | link
    confidence     REAL NOT NULL DEFAULT 0,
    release_id     TEXT,
    recording_id   TEXT,
    album          TEXT,
    title          TEXT NOT NULL DEFAULT '',
    artists        TEXT NOT NULL DEFAULT '',
    duration_delta REAL,
    payload_json   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX idx_tag_proposals_state ON tag_proposals(state, updated_at DESC);

-- 标签写入历史。整份文件备份对标签写入没有必要（音频逐字节不变），
-- 只需要记住「写之前这些字段是什么」，撤销时按字段还原即可。
CREATE TABLE tag_writes (
    id           INTEGER PRIMARY KEY,
    txn_id       TEXT NOT NULL,
    location_id  INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    before_json  TEXT NOT NULL,
    applied_json TEXT NOT NULL,
    audio_hash   TEXT,
    written_at   REAL NOT NULL,
    undone_at    REAL
);
CREATE INDEX idx_tag_writes_loc ON tag_writes(location_id, written_at DESC);
CREATE INDEX idx_tag_writes_txn ON tag_writes(txn_id);

-- 指纹缓存。fpcalc 是 CPU 密集操作（整首歌解码），
-- 文件没变就不该重算；用大小 + mtime_ns 判断，与音频哈希解耦。
CREATE TABLE fingerprints (
    location_id INTEGER PRIMARY KEY REFERENCES locations(id) ON DELETE CASCADE,
    file_size   INTEGER NOT NULL,
    file_mtime  INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    duration    REAL NOT NULL,
    result_json TEXT,
    created_at  REAL NOT NULL
);

-- 录音的作品/艺人关系缓存（作曲、作词、制作人等）。
-- 这些字段要额外查 1~2 次接口，而同一首录音每天都会被扫到；
-- 不缓存就得反复请求，全库一次扫描会多出几千次调用。
CREATE TABLE recording_credits (
    recording_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    fetched_at   REAL NOT NULL
);
"""

_MIGRATION_12 = """
-- 曲库整理（重复检测 / 按标签归档 / 空目录）。
--
-- 音频索引缓存：去重扫描要算「去掉标签后的压缩音频哈希」，全库重算代价高；
-- 文件大小与 mtime 没变就直接复用。它也是「同一录音」判定的证据来源。
CREATE TABLE audio_index (
    root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path      TEXT NOT NULL,
    file_size     INTEGER NOT NULL,
    file_mtime    INTEGER NOT NULL,
    media_format  TEXT NOT NULL,
    audio_hash    TEXT NOT NULL,
    duration      REAL,
    title         TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    recording_id  TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL,
    PRIMARY KEY (root_id, rel_path)
);
CREATE INDEX idx_audio_index_hash ON audio_index(root_id, audio_hash);
CREATE INDEX idx_audio_index_recording ON audio_index(root_id, recording_id);

-- 重复组。只呈现、不删除；每次扫描按当前磁盘事实整体重建。
-- items_json 里保存并排对比所需的全部字段（大小、格式、时长、标签、建议）。
CREATE TABLE dedupe_groups (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    group_key      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    keep_rel_path  TEXT NOT NULL,
    keep_reason    TEXT NOT NULL,
    items_json     TEXT NOT NULL,
    found_at       REAL NOT NULL,
    UNIQUE (root_id, group_key)
);
CREATE INDEX idx_dedupe_groups_root ON dedupe_groups(root_id, found_at DESC);

-- 整理操作日志：移动 / 改名 / 删除空目录（以及整目录移入回收站）。
-- 移动与改名不复制文件（那会把曲库体积翻倍），靠 old/new 路径逐条撤销；
-- 删除类操作把 trash_rel_path 指到回收站里的相对路径，可从回收站还原。
CREATE TABLE organize_ops (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    batch_id       TEXT NOT NULL,
    op_kind        TEXT NOT NULL,           -- move | delete | rmdir
    old_rel_path   TEXT NOT NULL,
    new_rel_path   TEXT,
    trash_rel_path TEXT,
    detail         TEXT,
    created_at     REAL NOT NULL,
    undone_at      REAL
);
CREATE INDEX idx_organize_ops_batch ON organize_ops(batch_id);
CREATE INDEX idx_organize_ops_at ON organize_ops(created_at DESC);

-- 预览计划。执行必须使用「用户看到的那一份」而不是重新算一遍：
-- 预览之后文件可能被别的程序改动，两套结果不一致时宁可拒绝执行。
CREATE TABLE organize_plans (
    id           TEXT PRIMARY KEY,
    root_id      INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    subpath      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at   REAL NOT NULL,
    consumed_at  REAL
);
CREATE INDEX idx_organize_plans_at ON organize_plans(created_at DESC);
"""

_MIGRATION_13 = """
-- 用户手动忽略的重复组。
--
-- 之前「忽略这组」是直接删行，下次扫描整体重建时又原样回来，等于没忽略。
-- 改成在组上记一个 dismissed_at：扫描重建时按 group_key 把忽略状态带过去，
-- 组里的文件组合变了（group_key 变）就重新判断。恢复时清空这个时间戳。
ALTER TABLE dedupe_groups ADD COLUMN dismissed_at REAL;
CREATE INDEX idx_dedupe_groups_dismissed ON dedupe_groups(root_id, dismissed_at);
"""

_MIGRATION_14 = """
-- 标签提案上的人工痕迹。
--
-- 之前「跳过」只有一个 state='skipped'，既看不见也恢复不了；更要命的是
-- 人工写入完成后提案行被**直接删除**，人工决定随之消失——下一轮扫描
-- 重新算出不一样的匹配，这条曲子又回到待审查，看起来像「人工确认没用」。
--
-- 现在把人工痕迹留在行上：
--   ignored —— 人工忽略，列表可见、可恢复，扫描不得覆盖；
--   done    —— 人工已定案（写入完成），不再复议，扫描不得覆盖；
--   dismissed_at —— 忽略时间，供「已忽略」列表排序与展示。
ALTER TABLE tag_proposals ADD COLUMN dismissed_at REAL;
UPDATE tag_proposals SET state='ignored' WHERE state='skipped';
CREATE INDEX idx_tag_proposals_dismissed ON tag_proposals(state, dismissed_at);
"""

_MIGRATION_15 = """
-- 给迁移 14 转过来的「忽略」记录补上忽略时间。
--
-- 迁移 14 把旧的 state='skipped' 改成了 'ignored'，但那些行从来没有
-- dismissed_at。忽略时间取 updated_at：那是当初点「跳过」的时刻，
-- 是现有数据里最接近的真实值。
UPDATE tag_proposals
   SET dismissed_at = updated_at
 WHERE state = 'ignored' AND dismissed_at IS NULL;
"""

_MIGRATION_16 = """
-- 身份解析结论（重构方案：一次定身份，驱动标签/歌词/封面/归置四支线）。
--
-- 只存「解析结论」，不存网络失败的重试计划——那仍是 resolutions 负缓存的职责，
-- 两者职责不重叠。input_hash 是解析输入（标签关键字段+文件名+目录名+标签MBID）
-- 的指纹：输入变了结论自动失效，下一轮重算，不需要人工触发。
CREATE TABLE identities (
    location_id      INTEGER PRIMARY KEY REFERENCES locations(id) ON DELETE CASCADE,
    state            TEXT NOT NULL,
    recording_id     TEXT NOT NULL DEFAULT '',
    release_id       TEXT NOT NULL DEFAULT '',
    release_group_id TEXT NOT NULL DEFAULT '',
    evidence         TEXT NOT NULL DEFAULT '',
    confidence       REAL,
    source           TEXT NOT NULL DEFAULT '',
    query_used       TEXT NOT NULL DEFAULT '',
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    input_hash       TEXT NOT NULL,
    resolved_at      REAL NOT NULL
);
CREATE INDEX idx_identities_state ON identities(state);
"""

_MIGRATION_17 = """
-- 专辑封面缓存：封面是发行属性不是曲目属性，按专辑聚合一次下载、逐文件嵌入。
-- 图片字节落在 data/cover-cache/（file_path），库里只存指纹与元数据，避免库膨胀。
CREATE TABLE cover_albums (
    id            INTEGER PRIMARY KEY,
    album_key     TEXT NOT NULL UNIQUE,
    source        TEXT NOT NULL,
    image_hash    TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    mime          TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    fetched_at    REAL NOT NULL,
    applied_count INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT 'ready'
);
CREATE INDEX idx_cover_albums_state ON cover_albums(state, fetched_at DESC);
"""

_MIGRATION_18 = """
-- 歌词的翻译、逐字原文与 AI 预审结论。
--
-- translation：上游（QQ trans / 网易 tlyric）一直有产出，但写链路从未消费，
-- 数据在源头被丢弃——先暂存下来，写入时按策略决定形态（双语/独立字段/忽略）。
-- raw_qrc：逐字歌词默认降级为行级 LRC，原文按策略可选保留（.qrc sidecar）。
-- ai_json：模型对候选的判定（best/confidence/reason/rejected）。复核界面直接
-- 展示这个结论；高置信且程序闸门全过时，允许自动采纳——判定留档可回溯。
ALTER TABLE staged_lyrics ADD COLUMN translation TEXT;
ALTER TABLE staged_lyrics ADD COLUMN raw_qrc TEXT;
ALTER TABLE staged_lyrics ADD COLUMN ai_json TEXT;
"""

_MIGRATION_19 = """
-- 任务步骤明细与来源健康。
--
-- task_steps：一次批量任务的「每文件 × 每环节」结果矩阵，失败不再只能翻日志，
-- 待办「失败」队列直接从这里取数。
-- provider_health：非官方来源（QQ/网易云）的每日健康计数，烟雾探测与自动化页
-- 健康卡共用；blocked/schema_error 单列，是「来源被风控/结构变更」的信号，
-- 与普通失败区分对待（不写负缓存、摘除降级）。
CREATE TABLE task_steps (
    id          INTEGER PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    location_id INTEGER,
    rel_path    TEXT,
    state       TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_task_steps_task ON task_steps(task_id, seq);

CREATE TABLE provider_health (
    provider     TEXT NOT NULL,
    day          TEXT NOT NULL,
    ok           INTEGER NOT NULL DEFAULT 0,
    not_found    INTEGER NOT NULL DEFAULT 0,
    blocked      INTEGER NOT NULL DEFAULT 0,
    schema_error INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    PRIMARY KEY (provider, day)
);
"""

_MIGRATION_20 = """
-- 受管根的显示名与宿主机路径。
--
-- display_name：界面显示名（用户在设置里取名，如「我的曲库」）。
-- host_path：宿主机上的真实路径。容器内路径是 /library，宿主机上却是
--   /vol2/1000/Music/Library——目录名（Library）与内部名（library）
--   大小写不一致时，按容器内路径推导显示名会得到「library」，与磁盘上
--   的真实目录对不上。宿主机路径才是用户认的那个名字。
ALTER TABLE roots ADD COLUMN display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE roots ADD COLUMN host_path TEXT NOT NULL DEFAULT '';
-- runtime=1 表示这个根是用户在设置页新增的（不在 lyra.toml 里）。
-- 配置文件里的根由 sync_root_profiles 登记，runtime 保持 0。
ALTER TABLE roots ADD COLUMN runtime INTEGER NOT NULL DEFAULT 0;
"""


_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, _MIGRATION_3),
    (4, _MIGRATION_4),
    (5, _MIGRATION_5),
    (6, _MIGRATION_6),
    (7, _MIGRATION_7),
    (8, _MIGRATION_8),
    (9, _MIGRATION_9),
    (10, _MIGRATION_10),
    (11, _MIGRATION_11),
    (12, _MIGRATION_12),
    (13, _MIGRATION_13),
    (14, _MIGRATION_14),
    (15, _MIGRATION_15),
    (16, _MIGRATION_16),
    (17, _MIGRATION_17),
    (18, _MIGRATION_18),
    (19, _MIGRATION_19),
    (20, _MIGRATION_20),
)


# ---------------------------------------------------------------------------
# 连接与迁移
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StateStore:
    """SQLite 状态库门面。

    **每线程一个连接。** FastAPI 会把同步端点丢到工作线程执行，
    而 sqlite3 默认禁止跨线程使用同一个连接
    （`SQLite objects created in a thread can only be used in that same thread`）。
    后台任务线程同理。因此连接按线程惰性创建；WAL 模式下多连接并发安全，
    写冲突由 `busy_timeout` 承担。

    单实例使用；**不要**把 WAL 数据库放在 SMB/NFS 共享路径上。
    """

    path: Path
    _local: threading.local = field(default_factory=threading.local)
    _all: list[sqlite3.Connection] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- 生命周期 ---------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,  # 自己管理事务
            # 只有「关闭别的线程创建的连接」需要它；实际使用仍由线程局部
            # 连接保证——每个线程只用自己创建的那个。
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=180000")
        with self._lock:
            self._all.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._new_connection()
            self._local.conn = existing
        return existing

    def connect(self) -> sqlite3.Connection:
        """兼容既有调用点；返回当前线程的连接。"""
        return self.conn

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                with suppress(sqlite3.Error):
                    conn.close()
            self._all.clear()
        self._local = threading.local()

    def __enter__(self) -> StateStore:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务。异常时回滚。

        对 `database is locked` 做**有界重试**。

        为什么光靠 `busy_timeout` 不够：`BEGIN IMMEDIATE` 在 SQLite 内部
        有「避免死锁」的判定路径，走那条路时 **busy handler 根本不会被调用**，
        于是即使把 `busy_timeout` 设到 180 秒也会立刻返回 SQLITE_BUSY。
        实际「一键批量写入」与常驻监控同时写这个库时就会触发。
        与其继续调参数，不如在应用层退避重试——这是可控且可验证的。
        """
        conn = self.conn
        delay = 0.05
        for attempt in range(8):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- 迁移 -------------------------------------------------------------

    def current_schema_version(self) -> int:
        conn = self.conn
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if row is None:
            return 0
        got = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(got["value"]) if got else 0

    def migrate(self, *, backup: bool = True) -> int:
        """把库升级到 SCHEMA_VERSION。返回执行后的版本。

        schema 变更前先备份（架构约束：迁移前备份，失败拒绝启动）。

        **跨进程串行化。** Web 与监控是两个容器、各自启动时都会迁移。
        实际两个进程同时启动时，A 建立 `write_waits` 并提交，B 在等待
        写锁之后仍按「当前版本 9」判断，于是重复执行 `CREATE TABLE`
        直接报 `table write_waits already exists` 崩溃。
        用文件锁把整段迁移串起来，并在拿到锁后**重新读版本**。
        """
        import fcntl

        lock_path = self.path.with_suffix(self.path.suffix + ".migrate.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock:
            try:  # noqa: SIM105
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except OSError:  # 平台不支持时退化为无锁（单进程场景仍正确）
                pass  # noqa: SIM105
            try:
                return self._migrate_locked(backup=backup)
            finally:
                with suppress(OSError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _migrate_locked(self, *, backup: bool) -> int:
        current = self.current_schema_version()
        if current == SCHEMA_VERSION:
            return current
        if current > SCHEMA_VERSION:
            raise DatabaseError(
                f"数据库 schema 版本 {current} 高于本程序支持的 {SCHEMA_VERSION}；"
                "请升级程序，不要用旧版打开新库"
            )

        if current > 0 and backup:
            self._backup_before_migration(current)

        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            self._apply_migration(version, script)
        return SCHEMA_VERSION

    def _apply_migration(self, version: int, script: str) -> None:
        """应用一个迁移，整体原子。

        不能用 `with self.transaction()` 包 `executescript`：Python 的
        executescript 会先隐式提交当前事务，导致外层 COMMIT 报
        "no transaction is active"。因此把事务控制写进脚本本身。

        version 来自本模块的常量元组，不是外部输入，无注入面。
        """
        conn = self.conn
        wrapped = (
            "BEGIN IMMEDIATE;\n"
            f"{script}\n"
            "INSERT INTO meta(key, value) VALUES('schema_version', "
            f"'{int(version)}') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value;\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(wrapped)
        except sqlite3.Error:
            # 脚本中途失败时事务可能仍开着，显式回滚，避免污染后续连接
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def _backup_before_migration(self, from_version: int) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = self.path.with_name(f"{self.path.name}.v{from_version}.{stamp}.bak")
        try:
            with sqlite3.connect(str(target)) as dst:
                self.conn.backup(dst)
        except sqlite3.Error as exc:
            raise DatabaseError(f"迁移前备份失败，拒绝继续: {exc}") from exc
        return target

    # -- meta -------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- roots ------------------------------------------------------------

    def upsert_root(
        self,
        *,
        name: str,
        role: str,
        path: str,
        enabled: bool = True,
        read_only: bool = False,
        display_name: str = "",
        host_path: str = "",
        runtime: bool = False,
    ) -> int:
        now = time.time()
        with self.transaction() as tx:
            row = tx.execute("SELECT id FROM roots WHERE name=?", (name,)).fetchone()
            if row:
                # 显示名与宿主机路径是**用户设置**：已填的不要被调用方
                # 的空值覆盖（每次扫描都会调 upsert_root）。
                tx.execute(
                    "UPDATE roots SET role=?, path=?, enabled=?, read_only=?, "
                    "display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END, "
                    "host_path=CASE WHEN ?<>'' THEN ? ELSE host_path END, "
                    "runtime=MAX(runtime, ?), updated_at=? WHERE id=?",
                    (
                        role, path, int(enabled), int(read_only),
                        display_name, display_name,
                        host_path, host_path,
                        int(runtime), now, row["id"],
                    ),
                )
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO roots(name, role, path, enabled, read_only, "
                "display_name, host_path, runtime, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, role, path, int(enabled), int(read_only),
                 display_name, host_path, int(runtime), now, now),
            )
            return int(cur.lastrowid or 0)

    def set_root_display(
        self,
        root_id: int,
        *,
        display_name: str = "",
        host_path: str = "",
    ) -> None:
        """保存受管根的显示名与宿主机路径（设置页可改）。"""
        self.conn.execute(
            "UPDATE roots SET display_name=?, host_path=?, updated_at=? WHERE id=?",
            (display_name, host_path, time.time(), root_id),
        )
        self.conn.commit()

    def get_root_id(self, name: str) -> int | None:
        row = self.conn.execute("SELECT id FROM roots WHERE name=?", (name,)).fetchone()
        return int(row["id"]) if row else None

    def set_root_health(self, root_id: int, health: str) -> None:
        self.conn.execute(
            "UPDATE roots SET health=?, updated_at=? WHERE id=?",
            (health, time.time(), root_id),
        )

    # -- locations --------------------------------------------------------

    def upsert_location(
        self,
        *,
        root_id: int,
        rel_path: str,
        size: int,
        mtime: float,
        ctime: float | None = None,
        inode: int | None = None,
        full_hash: str | None = None,
        asset_id: int | None = None,
    ) -> int:
        now = time.time()

        # **没变化就别写。** 监控每 30 秒整库对账一次：1810 首就是 1810 个
        # `BEGIN IMMEDIATE` 写事务，与正在写入的任务抢锁——实际一次 12 首的
        # 写入里 9 首被判「数据库持续被占用」而推迟。
        # 这里先在**只读**查询里判断：行还在、字段没变、最近 5 分钟内见过，
        # 就直接返回；只有真的变了才进写事务。
        row = self.conn.execute(
            "SELECT id, size, mtime, ctime, inode, full_hash, asset_id, "
            "missing_since, last_seen_at FROM locations "
            "WHERE root_id=? AND rel_path=?",
            (root_id, rel_path),
        ).fetchone()
        if row is not None and row["missing_since"] is None:
            same = (
                int(row["size"] or 0) == int(size)
                and abs(float(row["mtime"] or 0.0) - float(mtime)) < 1e-6
                and (ctime is None or row["ctime"] is None
                     or abs(float(row["ctime"]) - float(ctime)) < 1e-6)
                and (inode is None or row["inode"] is None
                     or int(row["inode"]) == int(inode))
                and (full_hash is None or row["full_hash"] == full_hash)
            )
            if same and (now - float(row["last_seen_at"] or 0.0)) < 300.0:
                return int(row["id"])

        with self.transaction() as tx:
            row = tx.execute(
                "SELECT id FROM locations WHERE root_id=? AND rel_path=?",
                (root_id, rel_path),
            ).fetchone()
            if row:
                tx.execute(
                    "UPDATE locations SET size=?, mtime=?, ctime=?, inode=?, full_hash=?, "
                    "asset_id=?, last_seen_at=?, missing_since=NULL WHERE id=?",
                    (size, mtime, ctime, inode, full_hash, asset_id, now, row["id"]),
                )
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO locations(root_id, rel_path, size, mtime, ctime, inode, "
                "full_hash, asset_id, last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (root_id, rel_path, size, mtime, ctime, inode, full_hash, asset_id, now),
            )
            return int(cur.lastrowid or 0)

    def mark_location_missing(self, location_id: int) -> None:
        """标记为「本次未见」。

        注意：**不得**据此删除记录。只有根健康且连续两次完整核对都确认不存在，
        才归档。
        """
        self.conn.execute(
            "UPDATE locations SET missing_since=COALESCE(missing_since, ?) WHERE id=?",
            (time.time(), location_id),
        )

    def iter_locations(self, root_id: int) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM locations WHERE root_id=? ORDER BY rel_path", (root_id,)
        )

    # -- assets -----------------------------------------------------------

    def upsert_asset(self, *, audio_hash: str, media_format: str, duration: float | None) -> int:
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT id FROM assets WHERE audio_hash=? AND media_format=?",
                (audio_hash, media_format),
            ).fetchone()
            if row:
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO assets(audio_hash, media_format, duration, created_at) "
                "VALUES(?,?,?,?)",
                (audio_hash, media_format, duration, time.time()),
            )
            return int(cur.lastrowid or 0)

    # -- 歌词观察 ---------------------------------------------------------

    def record_lyric_observation(
        self,
        *,
        location_id: int,
        embedded_state: str,
        embedded_fields: Sequence[str] = (),
        embedded_hash: str | None = None,
        sidecar_state: str | None = None,
        sidecar_hash: str | None = None,
        sidecar_size: int | None = None,
        sidecar_path: str | None = None,
        sidecar_names_differ: bool = False,
        has_embedded_cover: bool = False,
        issues: Sequence[str] = (),
    ) -> int:
        payload = (
            embedded_state,
            json.dumps(list(embedded_fields), ensure_ascii=False),
            embedded_hash,
            sidecar_state,
            sidecar_hash,
            sidecar_size,
            sidecar_path,
            int(sidecar_names_differ),
            int(has_embedded_cover),
            json.dumps(list(issues), ensure_ascii=False),
        )
        # **与上一条完全相同的观察不重复入库。**
        # 监控每 30 秒整库对账一次：204 首 × 每轮 204 行 = 每小时约 2.4 万行、
        # 每天约 58 万行，而其中绝大多数与上一条一字不差。这张表记录的是
        # 「状态变化史」，不是心跳。实际这一条把观察表从 2 千行涨到 15 万行
        # （约 6 小时），库文件与相关查询都跟着变慢。
        latest = self.conn.execute(
            "SELECT id, embedded_state, embedded_fields, embedded_hash, sidecar_state, "
            "sidecar_hash, sidecar_size, sidecar_path, sidecar_names_differ, "
            "has_embedded_cover, issues FROM lyric_observations "
            "WHERE location_id=? ORDER BY id DESC LIMIT 1",
            (location_id,),
        ).fetchone()
        if latest is not None and tuple(latest)[1:] == payload:
            return int(latest["id"])

        cur = self.conn.execute(
            "INSERT INTO lyric_observations(location_id, embedded_state, embedded_fields, "
            "embedded_hash, sidecar_state, sidecar_hash, sidecar_size, sidecar_path, "
            "sidecar_names_differ, has_embedded_cover, issues, observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (location_id, *payload, time.time()),
        )
        return int(cur.lastrowid or 0)

    def prune_observations(self, *, keep_per_location: int = 3) -> int:
        """只保留每个位置最近 N 条观察，返回删除的行数。

        去重之后历史只在真正变化时增长，但**旧版本留下的重复行还在**：
        实际某库一次运行就积累了 15 万行，而位置只有 204 个。
        这些行除了让库变大、让 `MAX(id)` 子查询变慢之外没有任何用处。
        """
        keep = max(1, int(keep_per_location))
        cur = self.conn.execute(
            """DELETE FROM lyric_observations
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY location_id ORDER BY id DESC
                               ) AS rank_in_location
                          FROM lyric_observations
                    ) WHERE rank_in_location <= ?
                )""",
            (keep,),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    # -- 任务 -------------------------------------------------------------

    def enqueue_job(
        self,
        *,
        job_key: str,
        job_type: str,
        root_id: int | None = None,
        location_id: int | None = None,
        priority: int = 0,
        next_run_at: float | None = None,
    ) -> int:
        """入队。已存在同 job_key 则保持原状态（幂等）。"""
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO jobs(job_key, job_type, root_id, location_id, state, priority, "
            "next_run_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_key) DO NOTHING",
            (
                job_key,
                job_type,
                root_id,
                location_id,
                JobState.PENDING,
                priority,
                next_run_at if next_run_at is not None else now,
                now,
                now,
            ),
        )
        if cur.rowcount:
            return int(cur.lastrowid or 0)
        row = self.conn.execute("SELECT id FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        return int(row["id"]) if row else 0

    def claim_job(self, *, owner: str, lease_seconds: float, now: float | None = None) -> sqlite3.Row | None:
        """领取一个到期任务（事务操作，避免重复提交同一计划）。

        **必须把「仍在租约内」的行排除在查询之外，而不是查出来再放弃。**
        否则只要有一个长租约任务排在最前（例如崩溃后残留的 1 小时租约），
        整个队列在它到期前都领不到任何任务——其余 pending 任务被活活饿死。
        """
        now = now if now is not None else time.time()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM jobs WHERE "
                "(state=? AND (next_run_at IS NULL OR next_run_at<=?)) OR "
                "(state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?) "
                "ORDER BY priority DESC, COALESCE(next_run_at, 0) ASC LIMIT 1",
                (JobState.PENDING, now, JobState.LEASED, now),
            ).fetchone()
            if row is None:
                return None
            tx.execute(
                "UPDATE jobs SET state=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1, "
                "updated_at=? WHERE id=?",
                (JobState.LEASED, owner, now + lease_seconds, now, row["id"]),
            )
            # 重新读一次：返回 UPDATE 之后的真实状态。
            # 旧快照里的 attempts/state/lease_expires_at 都是过期的，
            # 调用方据此做重试计数会算错。
            return tx.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()

    def finish_job(
        self,
        job_id: int,
        *,
        state: str,
        error_class: str | None = None,
        error: str | None = None,
        next_run_at: float | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET state=?, last_error_class=?, last_error=?, next_run_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
            (state, error_class, error, next_run_at, time.time(), job_id),
        )

    def recover_expired_leases(self, now: float | None = None) -> int:
        """进程崩溃后回收过期租约（可恢复，不重复提交同一计划）。"""
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
            (JobState.PENDING, now, JobState.LEASED, now),
        )
        return cur.rowcount or 0

    # -- 来源解析 / 负缓存 ------------------------------------------------

    def get_resolution(self, resolution_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM resolutions WHERE resolution_key=?", (resolution_key,)
        ).fetchone()

    def record_resolution(
        self,
        *,
        resolution_key: str,
        provider: str,
        outcome: str,
        asset_id: int | None = None,
        location_id: int | None = None,
        provider_id: str | None = None,
        candidate: dict[str, Any] | None = None,
        lyrics_hash: str | None = None,
        query_log: Sequence[dict[str, Any]] | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO resolutions(resolution_key, asset_id, location_id, provider, outcome, provider_id, "
            "candidate_json, lyrics_hash, query_log, attempts, next_retry_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?) "  # attempts 在第 10 列（location_id 插到第 3 列后移位了）
            "ON CONFLICT(resolution_key) DO UPDATE SET outcome=excluded.outcome, "
            "location_id=COALESCE(excluded.location_id, resolutions.location_id), "
            "provider_id=excluded.provider_id, candidate_json=excluded.candidate_json, "
            "lyrics_hash=excluded.lyrics_hash, query_log=excluded.query_log, "
            "attempts=resolutions.attempts+1, next_retry_at=excluded.next_retry_at, "
            "updated_at=excluded.updated_at",
            (
                resolution_key,
                asset_id, location_id,
                provider,
                outcome,
                provider_id,
                json.dumps(candidate, ensure_ascii=False) if candidate else None,
                lyrics_hash,
                json.dumps(list(query_log or []), ensure_ascii=False),
                next_retry_at,
                now,
                now,
            ),
        )

    def supersede_obsolete_reviews(self) -> int:
        """把「这一首已经有歌词了」的待审查记录标记为 superseded。

        ## 为什么需要

        实际发现：同一首曲目**已经写入过歌词**，却仍有一条旧的
        `needs_review` 记录挂在队列里。抽查 10 条「缺判定证据」的记录，
        全都是这种情况——它们是历次抓取留下的过期候选，
        对使用者是纯噪声：他以为这首歌还需要决定，实际早就写好了。

        ## 判定口径

        看的是**此刻文件里有什么**（`lyric_observations` 的最新一条）：
        已经有**真歌词**（带时间轴或纯文本）就不再需要决定。

        有意**保守**：
          - 只是「纯音乐占位文案」的曲目**保留**候选——占位不是「没有歌词」
            的证据，真歌词仍然可能把它换掉；
          - 「时间轴异常」的也保留——候选可能正是修好它的那一条。
        """
        cur = self.conn.execute(
            """
            UPDATE staged_lyrics
               SET state = 'superseded'
             WHERE state = 'needs_review'
               AND EXISTS (
                   SELECT 1 FROM lyric_observations obs
                    WHERE obs.location_id = staged_lyrics.location_id
                      AND obs.id = (SELECT MAX(id) FROM lyric_observations
                                    WHERE location_id = staged_lyrics.location_id)
                      AND (
                            obs.embedded_state IN ('synced', 'plain_text')
                         OR obs.sidecar_state  IN ('synced', 'plain_text')
                      )
               )
            """
        )
        return cur.rowcount or 0

    def purge_expired_negative_cache(self, now: float | None = None) -> int:
        """删除已过期且**允许**重试的负缓存。

        注意：`needs_review` 不是失败重试队列，
        它保存候选供离线复核，**不得**被当负缓存清理。
        """
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "DELETE FROM resolutions WHERE outcome IN (?, ?) AND next_retry_at IS NOT NULL "
            "AND next_retry_at<=?",
            (ResolutionOutcome.NOT_FOUND, ResolutionOutcome.NO_SYNCED_LYRICS, now),
        )
        return cur.rowcount or 0

    # -- 事务与 sidecar ---------------------------------------------------

    def new_txn_id(self) -> str:
        return uuid.uuid4().hex

    def begin_transaction_record(
        self,
        *,
        txn_id: str,
        location_id: int,
        plan: dict[str, Any],
        original_full_hash: str | None,
        original_audio_hash: str | None,
    ) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO transactions(txn_id, location_id, plan_json, state, "
            "original_full_hash, original_audio_hash, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                txn_id,
                location_id,
                json.dumps(plan, ensure_ascii=False),
                TransactionState.PREPARED,
                original_full_hash,
                original_audio_hash,
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    def set_transaction_state(
        self,
        txn_id: str,
        state: str,
        *,
        new_full_hash: str | None = None,
        new_audio_hash: str | None = None,
        backup_ref: str | None = None,
        staging_ref: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE transactions SET state=?, new_full_hash=COALESCE(?, new_full_hash), "
            "new_audio_hash=COALESCE(?, new_audio_hash), backup_ref=COALESCE(?, backup_ref), "
            "staging_ref=COALESCE(?, staging_ref), error=?, updated_at=? WHERE txn_id=?",
            (state, new_full_hash, new_audio_hash, backup_ref, staging_ref, error, time.time(), txn_id),
        )

    def prune_audit(
        self, *, keep_writes: int = 100, keep_transactions: int = 100
    ) -> dict[str, int]:
        """给「写入记录 / 事务记录」做滚动清理。

        这两张表只增不减：每次写入都会记一条，几个月后就是几万行。
        保留最近 N 条足够回溯，更早的没有价值（文件本身还在，备份也在）。

        **未完结的事务绝不清理**：它们可能还需要恢复，删掉就再也查不到了。
        """
        with self.transaction() as tx:
            writes = tx.execute(
                "DELETE FROM sidecar_writes WHERE id NOT IN ("
                "  SELECT id FROM sidecar_writes ORDER BY written_at DESC LIMIT ?)",
                (max(0, keep_writes),),
            ).rowcount
            txns = tx.execute(
                "DELETE FROM transactions WHERE state IN (?, ?, ?, ?) AND id NOT IN ("
                "  SELECT id FROM transactions ORDER BY updated_at DESC LIMIT ?)",
                (
                    TransactionState.COMPLETED, TransactionState.ROLLED_BACK,
                    TransactionState.RECOVERY_CONFLICT, TransactionState.SIDECAR_COMMITTED,
                    max(0, keep_transactions),
                ),
            ).rowcount
        return {"writes": max(0, writes), "transactions": max(0, txns)}

    def unfinished_transactions(self) -> list[sqlite3.Row]:
        """未完成事务，用于崩溃恢复。

        恢复必须**依实际哈希判断进度**，不能只信数据库最后一条状态。
        """
        # `recovery_conflict` 也是终态：目标既非原始、也非本事务结果，
        # 且无备份材料——已无从判定。早先漏了它，于是被标为终态的条目
        # 每次都重新出现在恢复报告里，报告久了就没人看了。
        return list(
            self.conn.execute(
                "SELECT * FROM transactions WHERE state NOT IN (?, ?, ?) "
                "ORDER BY updated_at",
                (
                    TransactionState.COMPLETED,
                    TransactionState.ROLLED_BACK,
                    TransactionState.RECOVERY_CONFLICT,
                ),
            )
        )

    def record_sidecar_write(
        self,
        *,
        sidecar_path: str,
        content_hash: str,
        created_new: bool,
        previous_hash: str | None,
        location_id: int | None = None,
        txn_id: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO sidecar_writes(txn_id, location_id, sidecar_path, content_hash, "
            "created_new, previous_hash, written_at) VALUES(?,?,?,?,?,?,?)",
            (
                txn_id,
                location_id,
                sidecar_path,
                content_hash,
                int(created_new),
                previous_hash,
                time.time(),
            ),
        )
        return int(cur.lastrowid or 0)

    def last_sidecar_write(self, sidecar_path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sidecar_writes WHERE sidecar_path=? AND undone_at IS NULL "
            "ORDER BY written_at DESC LIMIT 1",
            (sidecar_path,),
        ).fetchone()

    def mark_sidecar_undone(self, write_id: int) -> None:
        self.conn.execute(
            "UPDATE sidecar_writes SET undone_at=? WHERE id=?", (time.time(), write_id)
        )

    # -- 暂存歌词 ---------------------------------------------------------

    def stage_lyrics(
        self,
        *,
        location_id: int,
        content_hash: str,
        content: str,
        provider: str,
        provider_id: str | None = None,
        score: float | None = None,
        duration_delta: float | None = None,
        state: str = "staged",
        origin: str = "auto",
        translation: str | None = None,
        raw_qrc: str | None = None,
        ai_json: str | None = None,
    ) -> int:
        """暂存一份抓取到的歌词。

        同一位置同一内容重复暂存时**不重复插入**，保持幂等（更新判级与来源）。
        translation / raw_qrc / ai_json 是重构新增的附属数据：翻译供双语文体
        落盘，raw_qrc 供逐字歌词保留，ai_json 留档模型判定供复核界面展示。
        """
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT id FROM staged_lyrics WHERE location_id=? AND content_hash=?",
                (location_id, content_hash),
            ).fetchone()
            if row:
                tx.execute(
                    "UPDATE staged_lyrics SET provider=?, provider_id=?, score=?, "
                    "duration_delta=?, state=?, origin=?, "
                    "translation=COALESCE(?, translation), "
                    "raw_qrc=COALESCE(?, raw_qrc), "
                    # ai_json 直接覆写：旧判定对应上一轮候选集，本轮没有判定
                    # 时保留它会让复核界面展示一条从未做过的结论
                    "ai_json=? WHERE id=?",
                    (provider, provider_id, score, duration_delta, state,
                     origin, translation, raw_qrc, ai_json, row["id"]),
                )
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO staged_lyrics(location_id, content_hash, content, provider, "
                "provider_id, score, duration_delta, state, origin, translation, "
                "raw_qrc, ai_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    location_id,
                    content_hash,
                    content,
                    provider,
                    provider_id,
                    score,
                    duration_delta,
                    state,
                    origin,
                    translation,
                    raw_qrc,
                    ai_json,
                    time.time(),
                ),
            )
            return int(cur.lastrowid or 0)

    def replace_staged(
        self,
        *,
        staged_id: int,
        content_hash: str,
        content: str,
        provider: str,
        provider_id: str | None = None,
        score: float | None = None,
        duration_delta: float | None = None,
        translation: str | None = None,
        raw_qrc: str | None = None,
    ) -> int:
        """把**人工选定**的歌词写回**同一条**暂存记录（id 保持不变）。

        为什么就地更新而不是插入新行：
            插入新行会让复核项的 id 变化、于是它从「待审查」列表里消失——
            用户刚点完「用这个」，那条就找不到了，非常困惑。
            历史并不因此丢失：来源与决策都记在 resolutions，写入记在
            transactions。

        同时必须把**同位置其它**待写入行标记为 superseded：否则同一位置会有
        多份 `staged`，`run_apply` 会依次写入它们，白写一遍（还多一次备份），
        且最终内容取决于迭代顺序——那是不可接受的随机性。
        """
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT location_id FROM staged_lyrics WHERE id=?", (staged_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"暂存记录不存在: {staged_id}")
            location_id = int(row["location_id"])

            tx.execute(
                "UPDATE staged_lyrics SET state='superseded' "
                "WHERE location_id=? AND state='staged' AND id<>?",
                (location_id, staged_id),
            )
            # 唯一约束是 (location_id, content_hash)：同位置若已存在一份**内容相同**
            # 的历史行，直接改本行的 hash 会撞上它。同位置同内容本就等价，
            # 删掉那份重复行即可（它的状态不是 staged，没有待写入语义）。
            tx.execute(
                "DELETE FROM staged_lyrics "
                "WHERE location_id=? AND content_hash=? AND id<>?",
                (location_id, content_hash, staged_id),
            )
            tx.execute(
                # translation / raw_qrc 用新候选自带的（没有就置空）——
                # 就地更新会保留旧候选的翻译，双语合并会把错配的翻译行写进文件；
                # ai_json 直接清空：判定对应的是上一轮候选集，对新内容展示是误导。
                "UPDATE staged_lyrics SET content_hash=?, content=?, provider=?, "
                "provider_id=?, score=?, duration_delta=?, state='staged', "
                "origin='manual', translation=?, raw_qrc=?, ai_json=NULL, "
                "created_at=? WHERE id=?",
                (
                    content_hash,
                    content,
                    provider,
                    provider_id,
                    score,
                    duration_delta,
                    translation,
                    raw_qrc,
                    time.time(),
                    staged_id,
                ),
            )
            return staged_id

    def mark_reviewed(self, staged_id: int) -> None:
        """把该条标为「人工确认过」。

        写入时据此决定要不要整份备份：人工看过的这次备份很值，
        而系统自动接受的成千上万次不值。复核端点的 accept 必须调它。
        """
        self.conn.execute(
            "UPDATE staged_lyrics SET origin='review' WHERE id=?", (staged_id,)
        )

    # -- 跨进程写租约 -------------------------------------------------------

    def acquire_write_lock(
        self, name: str, owner: str, *, ttl_seconds: float = 900.0,
        wait_seconds: float = 0.0,
    ) -> bool:
        """尝试取得写租约。已被别人持有且未过期时返回 False。

        用 `expires_at` 而不是只靠显式释放：进程被 kill 时不可能执行释放，
        没有过期机制就会永久卡死后续所有写入。

        `wait_seconds > 0` 时轮询等待：人工操作不该因为监控正在跑就被拒绝
        ——监控会在每写完一个文件后检查等待队列并主动让出（见
        `request_write`）。
        """
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            now = time.time()
            with self.transaction() as tx:
                row = tx.execute(
                    "SELECT owner, expires_at FROM write_locks WHERE name=?", (name,)
                ).fetchone()
                if row is None or row["expires_at"] <= now or row["owner"] == owner:
                    tx.execute(
                        "INSERT INTO write_locks(name, owner, acquired_at, expires_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                        "owner=excluded.owner, acquired_at=excluded.acquired_at, "
                        "expires_at=excluded.expires_at",
                        (name, owner, now, now + ttl_seconds),
                    )
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    # -- 人工写入请求：让监控主动让出 --

    def clear_write_waits(self) -> int:
        """清空所有等待登记。只应在进程启动时调用。"""
        with self.transaction() as tx:
            cur = tx.execute("DELETE FROM write_waits")
            return cur.rowcount or 0

    def request_write(self, owner: str) -> None:
        """登记「有人在等写入」。监控看到这一行就会提前收手。"""
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO write_waits(owner, requested_at) VALUES(?,?) "
                "ON CONFLICT(owner) DO UPDATE SET requested_at=excluded.requested_at",
                (owner, time.time()),
            )

    def cancel_write_request(self, owner: str) -> None:
        with self.transaction() as tx:
            tx.execute("DELETE FROM write_waits WHERE owner=?", (owner,))

    def waiting_writers(self) -> list[str]:
        """当前在等写入的 owner。超过 5 分钟的登记视为残留（进程被杀）。

        人工租约最多等 120 秒，正常不会超过这个时长；进程被重启时
        finally 不会执行，旧登记必须能自动过期，否则自动写入会一直让出。
        """
        cutoff = time.time() - 300
        with self.transaction() as tx:
            tx.execute("DELETE FROM write_waits WHERE requested_at < ?", (cutoff,))
            rows = tx.execute(
                "SELECT owner FROM write_waits ORDER BY requested_at"
            ).fetchall()
        return [row["owner"] for row in rows]

    def renew_write_lock(self, name: str, owner: str, *, ttl_seconds: float = 900.0) -> None:
        """续租。长任务必须定期续，否则会在跑到一半时被别人的租约顶掉。"""
        with self.transaction() as tx:
            tx.execute(
                "UPDATE write_locks SET expires_at=? WHERE name=? AND owner=?",
                (time.time() + ttl_seconds, name, owner),
            )

    def release_write_lock(self, name: str, owner: str) -> None:
        with self.transaction() as tx:
            tx.execute(
                "DELETE FROM write_locks WHERE name=? AND owner=?", (name, owner)
            )

    def release_all_write_locks(self) -> int:
        """清空所有写租约。**进程启动时调用。**

        重启后不可能还有任务在运行，但库里的租约还在（并且有最长一小时的 TTL），
        于是新的写入任务会一直被拒绝——实际因此所有刮削都失败，
        而错误信息只说「已有任务在进行中」，很难定位。
        """
        with self.transaction() as tx:
            cur = tx.execute("DELETE FROM write_locks")
            return cur.rowcount or 0

    def log_watch_event(
        self, stage: str, detail: str = "", counters: dict[str, int] | None = None
    ) -> None:
        """记一条监控活动。只记**有意义的事件**，不是每次心跳都记。"""
        self.conn.execute(
            "INSERT INTO watch_log(at, stage, detail, counters_json) VALUES(?,?,?,?)",
            (time.time(), stage, detail,
             json.dumps(counters, ensure_ascii=False) if counters else None),
        )

    def recent_watch_events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT at, stage, detail, counters_json FROM watch_log "
            "ORDER BY at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            counters: dict[str, Any] = {}
            if r["counters_json"]:
                try:
                    parsed = json.loads(r["counters_json"])
                    if isinstance(parsed, dict):
                        counters = parsed
                except (TypeError, ValueError):
                    pass
            out.append({
                "at": r["at"], "stage": r["stage"],
                "detail": r["detail"] or "", "counters": counters,
            })
        return out

    def prune_watch_log(self, keep_days: float = 14.0) -> int:
        """只保留最近若干天。日志无限增长会拖慢查询，也没有长期价值。"""
        cur = self.conn.execute(
            "DELETE FROM watch_log WHERE at < ?", (time.time() - keep_days * 86400,)
        )
        return cur.rowcount or 0

    def write_lock_holder(self, name: str) -> str | None:
        """返回当前持有者；已过期或无人持有则返回 None。"""
        row = self.conn.execute(
            "SELECT owner, expires_at FROM write_locks WHERE name=?", (name,)
        ).fetchone()
        if row is None or row["expires_at"] <= time.time():
            return None
        return str(row["owner"])

    # -- 后台任务 -----------------------------------------------------------

    def upsert_task(
        self,
        *,
        task_id: str,
        kind: str,
        state: str,
        progress: str = "",
        result: object | None = None,
        error: str | None = None,
        started_at: float,
        finished_at: float | None = None,
    ) -> None:
        """写入/更新一条任务记录。

        每次状态变化都落库：进度也要写，否则重启后只能看到「在跑」却没有进度。
        """
        payload = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO tasks(id, kind, state, progress, result_json, error, "
                "started_at, finished_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
                "progress=excluded.progress, result_json=excluded.result_json, "
                "error=excluded.error, finished_at=excluded.finished_at",
                (task_id, kind, state, progress, payload, error, started_at, finished_at),
            )

    def list_tasks(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (int(limit),)
            )
        )

    def mark_interrupted_tasks(self) -> int:
        """把上一次进程遗留的 `running` 任务标为 `interrupted`。

        进程被杀时任务不可能跑到 finally，所以库里会留下永久 running 的记录。
        启动时清理一次，让界面能明确说「这个任务被重启打断了」。
        """
        with self.transaction() as tx:
            cur = tx.execute(
                "UPDATE tasks SET state='interrupted', finished_at=?, "
                "error=COALESCE(error, '进程重启导致中断') WHERE state='running'",
                (time.time(),),
            )
            return cur.rowcount or 0

    def dedupe_staged(self) -> int:
        """同一位置只保留**最新一份**待写入歌词，其余标记为 superseded。

        为什么必须有这个方法：历史上每次抓取都会 `stage_lyrics` 插入新行，
        于是同一位置会积累多份 `staged`。`run_apply` 会依次写入它们——
        白写一遍、多一次备份，而且最终内容取决于迭代顺序（不可接受的随机性）。
        实际实际运行确实出现过 9 个位置各有 2 份以上。

        返回被作废的行数。
        """
        with self.transaction() as tx:
            rows = tx.execute(
                "SELECT location_id, MAX(id) AS keep_id FROM staged_lyrics "
                "WHERE state='staged' GROUP BY location_id HAVING COUNT(*) > 1"
            ).fetchall()
            removed = 0
            for row in rows:
                cur = tx.execute(
                    "UPDATE staged_lyrics SET state='superseded' "
                    "WHERE location_id=? AND state='staged' AND id<>?",
                    (int(row["location_id"]), int(row["keep_id"])),
                )
                removed += cur.rowcount or 0
            return removed

    def supersede_staged_except(self, location_id: int, keep_id: int) -> int:
        """把同一位置**其它**待写入行标记为 superseded，返回作废行数。

        人工在界面上选定了一条候选时，同位置可能还留着历史抓取留下的
        其它待写入行。不作废它们的后果有两个：批量写入会把旧的那条
        也写一遍（多一次备份），最终内容取决于迭代顺序——那是不可接受的
        随机性。
        """
        with self.transaction() as tx:
            cur = tx.execute(
                "UPDATE staged_lyrics SET state='superseded' "
                "WHERE location_id=? AND state='staged' AND id<>?",
                (int(location_id), int(keep_id)),
            )
            return cur.rowcount or 0

    def staged_for_location(self, location_id: int) -> sqlite3.Row | None:
        """取该位置最新一份仍处于 staged 的歌词。"""
        return self.conn.execute(
            "SELECT * FROM staged_lyrics WHERE location_id=? AND state='staged' "
            "ORDER BY created_at DESC LIMIT 1",
            (location_id,),
        ).fetchone()

    def iter_staged(self, *, limit: int | None = None) -> Iterator[sqlite3.Row]:
        sql = (
            "SELECT s.*, l.rel_path, l.root_id FROM staged_lyrics s "
            "JOIN locations l ON l.id = s.location_id "
            "WHERE s.state='staged' ORDER BY s.created_at"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        yield from self.conn.execute(sql)

    def set_staged_state(self, staged_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE staged_lyrics SET state=? WHERE id=?", (state, staged_id)
        )

    def replace_staged_content(
        self,
        staged_id: int,
        *,
        content: str,
        content_hash: str,
        provider: str,
        provider_id: str | None,
        score: float | None,
        duration_delta: float | None,
        ai_json: str,
    ) -> None:
        """把一条暂存歌词整体换成另一个候选的词。

        用途：模型预审确认「同一录音」但选中的候选与程序按分数暂存的
        那条不同——换上模型选中的候选的词，再走 `mark_ai_reviewed` 的
        采纳路径。原地替换而不是另插一行：同一位置留两份候选（一份
        needs_review 一份 staged）会让复核队列永远挂着一条已经无意义的旧记录。
        """
        self.conn.execute(
            "UPDATE staged_lyrics SET content=?, content_hash=?, provider=?, "
            "provider_id=?, score=?, duration_delta=?, ai_json=? WHERE id=?",
            (content, content_hash, provider, provider_id, score,
             duration_delta, ai_json, staged_id),
        )
        self.conn.commit()

    def mark_ai_reviewed(
        self, staged_id: int, *, ai_json: str, adopt: bool
    ) -> None:
        """记录模型判定结果；`adopt=True` 时把条目转为待写入（origin='ai'）。

        origin 记成 ai：写入时走**完整备份**（人工确认过的才免备份，
        模型判定的留个后路），且写入记录可撤销。
        """
        if adopt:
            self.conn.execute(
                "UPDATE staged_lyrics SET state='staged', origin='ai', ai_json=? "
                "WHERE id=?",
                (ai_json, staged_id),
            )
        else:
            self.conn.execute(
                "UPDATE staged_lyrics SET ai_json=? WHERE id=?", (ai_json, staged_id)
            )

    def discard_stale_staged(self, *, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        cur = self.conn.execute(
            "DELETE FROM staged_lyrics WHERE state!='staged' AND created_at<?", (cutoff,)
        )
        return cur.rowcount or 0

    # -- 身份解析（重构方案：一次定身份，驱动标签/歌词/封面/归置） --------

    def upsert_identity(
        self,
        *,
        location_id: int,
        state: str,
        recording_id: str = "",
        release_id: str = "",
        release_group_id: str = "",
        evidence: str = "",
        confidence: float | None = None,
        source: str = "",
        query_used: str = "",
        evidence_json: str = "{}",
        input_hash: str,
    ) -> None:
        """写入/更新身份结论。input_hash 变了才重算——这里是幂等入口。"""
        now = time.time()
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO identities(location_id, state, recording_id, release_id, "
                "release_group_id, evidence, confidence, source, query_used, "
                "evidence_json, input_hash, resolved_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(location_id) DO UPDATE SET "
                "state=excluded.state, recording_id=excluded.recording_id, "
                "release_id=excluded.release_id, "
                "release_group_id=excluded.release_group_id, "
                "evidence=excluded.evidence, confidence=excluded.confidence, "
                "source=excluded.source, query_used=excluded.query_used, "
                "evidence_json=excluded.evidence_json, input_hash=excluded.input_hash, "
                "resolved_at=excluded.resolved_at",
                (location_id, state, recording_id, release_id, release_group_id,
                 evidence, confidence, source, query_used, evidence_json,
                 input_hash, now),
            )

    def get_identity(self, location_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM identities WHERE location_id=?", (location_id,)
        ).fetchone()

    # -- 专辑封面缓存 ------------------------------------------------------

    def upsert_cover_album(
        self,
        *,
        album_key: str,
        source: str,
        image_hash: str,
        file_path: str,
        mime: str,
        width: int,
        height: int,
    ) -> int:
        now = time.time()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT id FROM cover_albums WHERE album_key=?", (album_key,)
            ).fetchone()
            if row:
                tx.execute(
                    "UPDATE cover_albums SET source=?, image_hash=?, file_path=?, "
                    "mime=?, width=?, height=?, fetched_at=?, state='ready' WHERE id=?",
                    (source, image_hash, file_path, mime, width, height, now,
                     row["id"]),
                )
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO cover_albums(album_key, source, image_hash, file_path, "
                "mime, width, height, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                (album_key, source, image_hash, file_path, mime, width, height, now),
            )
            return int(cur.lastrowid or 0)

    def get_cover_album(self, album_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM cover_albums WHERE album_key=?", (album_key,)
        ).fetchone()

    def bump_cover_applied(self, album_key: str) -> None:
        self.conn.execute(
            "UPDATE cover_albums SET applied_count = applied_count + 1 WHERE album_key=?",
            (album_key,),
        )

    # -- 任务步骤明细（每文件 × 每环节的结果矩阵） --------------------------

    def add_task_step(
        self,
        *,
        task_id: str,
        seq: int,
        kind: str,
        location_id: int | None = None,
        rel_path: str | None = None,
        state: str,
        detail: dict | None = None,
    ) -> None:
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO task_steps(task_id, seq, kind, location_id, rel_path, "
                "state, detail_json) VALUES(?,?,?,?,?,?,?)",
                (task_id, seq, kind, location_id, rel_path, state,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )

    def iter_task_steps(self, task_id: str) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM task_steps WHERE task_id=? ORDER BY seq", (task_id,)
        )

    def failed_task_steps(self, *, limit: int = 200) -> list[sqlite3.Row]:
        """最近失败的步骤，供待办「失败」队列取数。root 经 rel_path 反查。"""
        return list(
            self.conn.execute(
                "SELECT s.task_id, s.seq, s.kind, s.rel_path, s.state, s.detail_json, "
                "t.kind AS task_kind, t.started_at, "
                "(SELECT r.name FROM locations l JOIN roots r ON r.id = l.root_id "
                " WHERE l.rel_path = s.rel_path LIMIT 1) AS root_name "
                "FROM task_steps s "
                "JOIN tasks t ON t.id = s.task_id "
                "WHERE s.state='failed' ORDER BY t.started_at DESC, s.seq LIMIT ?",
                (int(limit),),
            )
        )

    # -- 来源健康（非官方来源的每日计数与风控信号） -------------------------

    def bump_provider_health(
        self,
        provider: str,
        *,
        ok: bool = False,
        not_found: bool = False,
        blocked: bool = False,
        schema_error: bool = False,
        error: str = "",
    ) -> None:
        """按「今天」累计一次结果。day 用本地日期，健康卡按天展示。"""
        day = time.strftime("%Y-%m-%d")
        column = (
            "ok" if ok else "not_found" if not_found
            else "blocked" if blocked else "schema_error"
        )
        # 成功的探测/请求证明来源已恢复：清掉 last_error，
        # 否则修复后当天仍显示旧错误，误导排查
        error_expr = (
            "last_error=NULL" if ok
            else "last_error=CASE WHEN excluded.last_error IS NOT NULL "
            "THEN excluded.last_error ELSE provider_health.last_error END"
        )
        with self.transaction() as tx:
            tx.execute(
                f"INSERT INTO provider_health(provider, day, {column}, last_error) "
                "VALUES(?,?,1,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET "
                f"{column} = {column} + 1, {error_expr}",
                (provider, day, error or None),
            )

    def provider_health_summary(self, *, days: int = 30) -> list[dict]:
        cutoff = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - days * 86400)
        )
        rows = self.conn.execute(
            "SELECT provider, day, ok, not_found, blocked, schema_error, last_error "
            "FROM provider_health WHERE day >= ? ORDER BY provider, day",
            (cutoff,),
        ).fetchall()
        summary: dict[str, dict] = {}
        for row in rows:
            entry = summary.setdefault(
                row["provider"],
                {"provider": row["provider"], "ok": 0, "not_found": 0,
                 "blocked": 0, "schema_error": 0, "last_error": None, "days": []},
            )
            for key in ("ok", "not_found", "blocked", "schema_error"):
                entry[key] += int(row[key] or 0)
            if row["last_error"]:
                entry["last_error"] = row["last_error"]
            entry["days"].append(
                {"day": row["day"], "ok": int(row["ok"] or 0),
                 "not_found": int(row["not_found"] or 0),
                 "blocked": int(row["blocked"] or 0),
                 "schema_error": int(row["schema_error"] or 0)}
            )
        return list(summary.values())

    # -- 标签刮削 ---------------------------------------------------------

    def upsert_tag_proposal(
        self,
        *,
        location_id: int,
        state: str,
        source: str,
        confidence: float,
        title: str,
        artists: str,
        album: str = "",
        release_id: str = "",
        recording_id: str = "",
        duration_delta: float | None = None,
        payload: dict[str, Any],
        revive_skipped: bool = False,
    ) -> int:
        """写入/覆盖某个位置的当前提案。

        **人工痕迹不可被自动扫描覆盖**：

        - `ignored`（人工忽略）与 `done`（人工已定案）代表使用者已经表过态，
          下一次自动扫描又把它翻出来会让人以为操作没生效；
        - `source='link'` 是人工断言「就是这一条」，自动扫描不能把它覆盖回
          搜索结果，否则刚指定的来源会在下一轮悄悄消失。

        只有显式重新扫描（`revive_skipped=True`）才恢复。
        """
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False)
        row = self.conn.execute(
            "SELECT id, state, source FROM tag_proposals WHERE location_id=?",
            (location_id,),
        ).fetchone()
        if row is not None:
            if str(row["state"]) in ("skipped", "ignored", "done") and not revive_skipped:
                return int(row["id"])
            # 链接是人工断言「就是这一条」。自动扫描不能把它覆盖回搜索
            # 结果，否则使用者刚指定的来源会在下一轮悄悄消失。
            if row["source"] == "link" and not revive_skipped:
                return int(row["id"])
            self.conn.execute(
                "UPDATE tag_proposals SET state=?, source=?, confidence=?, release_id=?, "
                "recording_id=?, album=?, title=?, artists=?, duration_delta=?, "
                "payload_json=?, dismissed_at=NULL, updated_at=? WHERE location_id=?",
                (
                    state, source, float(confidence), release_id or None,
                    recording_id or None, album or None, title, artists,
                    duration_delta, payload_json, now, location_id,
                ),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO tag_proposals(location_id, state, source, confidence, release_id, "
            "recording_id, album, title, artists, duration_delta, payload_json, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                location_id, state, source, float(confidence), release_id or None,
                recording_id or None, album or None, title, artists,
                duration_delta, payload_json, now, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def tag_proposal(self, proposal_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT p.*, l.rel_path, l.root_id, r.name AS root_name "
            "FROM tag_proposals p JOIN locations l ON l.id=p.location_id "
            "JOIN roots r ON r.id=l.root_id WHERE p.id=?",
            (proposal_id,),
        ).fetchone()

    def tag_proposal_for_location(self, location_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tag_proposals WHERE location_id=?", (location_id,)
        ).fetchone()

    def iter_tag_proposals(
        self,
        *,
        state: str | None = None,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """按状态/关键词取提案。列表要分页：全库一次几千条，前端会卡。

        `query` 同时匹配曲名、艺人、专辑与路径——使用者记得的往往是
        「那个文件夹」或「那首歌」，不一定记得专辑名。
        """
        where, params = self._tag_proposal_filter(state=state, query=query)
        params = [*params, max(1, int(limit)), max(0, int(offset))]
        return self.conn.execute(
            "SELECT p.*, l.rel_path, l.root_id, r.name AS root_name "
            "FROM tag_proposals p JOIN locations l ON l.id=p.location_id "
            "JOIN roots r ON r.id=l.root_id "
            f"WHERE {where} AND l.missing_since IS NULL "
            "ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

    def count_tag_proposals(self, *, state: str | None = None, query: str = "") -> int:
        where, params = self._tag_proposal_filter(state=state, query=query)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tag_proposals p "
            "JOIN locations l ON l.id=p.location_id "
            f"WHERE {where} AND l.missing_since IS NULL",
            params,
        ).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def _tag_proposal_filter(
        *, state: str | None, query: str
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("p.state = ?")
            params.append(state)
        else:
            # 默认只给「还需要人看一眼」的队列。人工忽略（ignored）与
            # 人工已定案（done）不在其中，各自另有入口。
            clauses.append("p.state NOT IN ('ignored','done')")
        needle = (query or "").strip()
        if needle:
            pattern = f"%{needle}%"
            clauses.append(
                "(p.title LIKE ? OR p.artists LIKE ? OR p.album LIKE ? "
                "OR l.rel_path LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        return " AND ".join(clauses), params

    def tag_proposal_counts(self) -> dict[str, int]:
        """各状态的提案数。`done`（人工已定案）不计入——它不再是待办。"""
        rows = self.conn.execute(
            "SELECT p.state, COUNT(*) AS n FROM tag_proposals p "
            "JOIN locations l ON l.id=p.location_id "
            "WHERE p.state != 'done' AND l.missing_since IS NULL "
            "GROUP BY p.state"
        ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def set_tag_proposal_state(self, proposal_id: int, state: str) -> None:
        self.conn.execute(
            "UPDATE tag_proposals SET state=?, updated_at=? WHERE id=?",
            (state, time.time(), proposal_id),
        )
        self.conn.commit()

    def dismiss_tag_proposal(self, proposal_id: int) -> None:
        """人工忽略：出队，但留下痕迹，可以恢复。

        扫描不再覆盖它（`upsert_tag_proposal` 认得 ignored/done），
        所以「忽略」是真的忽略，不是「这一轮先放着」。
        """
        now = time.time()
        self.conn.execute(
            "UPDATE tag_proposals SET state='ignored', dismissed_at=?, updated_at=? "
            "WHERE id=?",
            (now, now, proposal_id),
        )
        self.conn.commit()

    def restore_tag_proposal(self, proposal_id: int) -> None:
        """恢复一条被忽略的提案：回到待审查（或未匹配），重新进入队列。"""
        row = self.conn.execute(
            "SELECT payload_json FROM tag_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        state = "unmatched"
        if row is not None:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            match = payload.get("match") if isinstance(payload, dict) else None
            if isinstance(match, dict) and str(match.get("source") or ""):
                state = "review"
        self.conn.execute(
            "UPDATE tag_proposals SET state=?, dismissed_at=NULL, updated_at=? WHERE id=?",
            (state, time.time(), proposal_id),
        )
        self.conn.commit()

    def delete_tag_proposal(self, proposal_id: int) -> None:
        self.conn.execute("DELETE FROM tag_proposals WHERE id=?", (proposal_id,))
        self.conn.commit()

    def record_tag_write(
        self,
        *,
        txn_id: str,
        location_id: int,
        before: dict[str, Sequence[str]],
        applied: dict[str, Sequence[str]],
        audio_hash: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO tag_writes(txn_id, location_id, before_json, applied_json, "
            "audio_hash, written_at) VALUES(?,?,?,?,?,?)",
            (
                txn_id,
                location_id,
                json.dumps({k: list(v) for k, v in before.items()}, ensure_ascii=False),
                json.dumps({k: list(v) for k, v in applied.items()}, ensure_ascii=False),
                audio_hash,
                time.time(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def iter_tag_writes(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT w.*, l.rel_path, r.name AS root_name "
            "FROM tag_writes w JOIN locations l ON l.id=w.location_id "
            "JOIN roots r ON r.id=l.root_id "
            "ORDER BY w.written_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()

    def tag_write(self, write_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT w.*, l.rel_path, l.root_id, r.name AS root_name "
            "FROM tag_writes w JOIN locations l ON l.id=w.location_id "
            "JOIN roots r ON r.id=l.root_id WHERE w.id=?",
            (write_id,),
        ).fetchone()

    def mark_tag_write_undone(self, write_id: int) -> None:
        self.conn.execute(
            "UPDATE tag_writes SET undone_at=? WHERE id=?", (time.time(), write_id)
        )
        self.conn.commit()

    def cached_fingerprint(
        self, location_id: int, *, file_size: int, file_mtime: int
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM fingerprints WHERE location_id=? AND file_size=? "
            "AND file_mtime=?",
            (location_id, int(file_size), int(file_mtime)),
        ).fetchone()

    def save_fingerprint(
        self,
        *,
        location_id: int,
        file_size: int,
        file_mtime: int,
        fingerprint: str,
        duration: float,
        result_json: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fingerprints(location_id, file_size, file_mtime, fingerprint, "
            "duration, result_json, created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(location_id) DO UPDATE SET file_size=excluded.file_size, "
            "file_mtime=excluded.file_mtime, fingerprint=excluded.fingerprint, "
            "duration=excluded.duration, result_json=excluded.result_json, "
            "created_at=excluded.created_at",
            (
                location_id, int(file_size), int(file_mtime), fingerprint,
                float(duration), result_json, time.time(),
            ),
        )
        self.conn.commit()

    def cached_recording_credits(self, recording_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM recording_credits WHERE recording_id=?",
            (recording_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save_recording_credits(
        self, recording_id: str, payload: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "INSERT INTO recording_credits(recording_id, payload_json, fetched_at) "
            "VALUES(?,?,?) ON CONFLICT(recording_id) DO UPDATE SET "
            "payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
            (recording_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    # -- 重命名 -----------------------------------------------------------

    def record_rename(
        self,
        *,
        root_id: int,
        old_rel_path: str,
        new_rel_path: str,
        reason: str,
        media_format: str,
        location_id: int | None = None,
        detail: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO renames(root_id, location_id, old_rel_path, new_rel_path, "
            "reason, media_format, detail, renamed_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                root_id,
                location_id,
                old_rel_path,
                new_rel_path,
                reason,
                media_format,
                detail,
                time.time(),
            ),
        )
        return int(cur.lastrowid or 0)

    def last_rename(self, root_id: int, old_rel_path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM renames WHERE root_id=? AND old_rel_path=? AND undone_at IS NULL "
            "ORDER BY renamed_at DESC LIMIT 1",
            (root_id, old_rel_path),
        ).fetchone()

    def iter_renames(self, *, active_only: bool = True) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM renames"
        if active_only:
            sql += " WHERE undone_at IS NULL"
        sql += " ORDER BY renamed_at DESC"
        yield from self.conn.execute(sql)

    def mark_rename_undone(self, rename_id: int) -> None:
        self.conn.execute(
            "UPDATE renames SET undone_at=? WHERE id=?", (time.time(), rename_id)
        )

    def location_by_rel_path(self, root_id: int, rel_path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM locations WHERE root_id=? AND rel_path=?", (root_id, rel_path)
        ).fetchone()

    def update_location_path(self, location_id: int, new_rel_path: str) -> None:
        """重命名后同步状态库路径。

        不这样做的话，下次扫描会把它当成「新文件」而把旧路径当成「消失」，
        负缓存与歌词观察状态都会错位。

        **目标路径可能已经有行**：整理把文件从脏目录（如
        `G.E.M. 鄧紫棋; G.E.M.鄧紫棋/…`）移到规范目录时，扫描早就为
        新路径登记过位置行——直接改路径会撞 `UNIQUE(root_id, rel_path)`
        约束，整个整理接口报 500（旧实现正是如此）。

        此时的正确处理是**合并**：新路径的行是「这个文件在磁盘上确实
        存在」的记录（存活），本次移动的旧行携带的是同一首歌的历史
        状态（歌词观察、暂存、标签提案）。把旧行的附属数据迁到存活行、
        删除旧行，既保住状态又不留重复。
        """
        now = time.time()
        row = self.conn.execute(
            "SELECT root_id FROM locations WHERE id=?", (location_id,)
        ).fetchone()
        if row is None:
            return
        root_id = int(row["root_id"])
        existing = self.conn.execute(
            "SELECT id FROM locations WHERE root_id=? AND rel_path=? AND id<>?",
            (root_id, new_rel_path, location_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "UPDATE locations SET rel_path=?, missing_since=NULL, "
                "last_seen_at=? WHERE id=?",
                (new_rel_path, now, location_id),
            )
            return

        # 合并：附属数据全部 ON DELETE CASCADE，删旧行会带走它们，
        # 所以先把它们迁到存活行，再删旧行。
        keep_id = int(existing["id"])
        self.conn.execute(
            "UPDATE locations SET missing_since=NULL, last_seen_at=? WHERE id=?",
            (now, keep_id),
        )
        for table in (
            "lyric_observations", "staged_lyrics", "tag_proposals",
            "resolutions", "approvals", "identities", "assets",
        ):
            try:
                self.conn.execute(
                    f"UPDATE {table} SET location_id=? WHERE location_id=?",
                    (keep_id, location_id),
                )
            except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
                # 目标行已有同键附属数据（如两条内容相同的暂存歌词）：
                # 直接迁会撞唯一约束。此时**保留目标行的那份**，删掉
                # 旧行的重复份——两者指向同一首歌的同一份内容，
                # 留一份就够，删的是重复不是数据。
                if "UNIQUE" not in str(exc):
                    continue
                self._drop_duplicate_children(table, location_id, keep_id)
                try:
                    self.conn.execute(
                        f"UPDATE {table} SET location_id=? WHERE location_id=?",
                        (keep_id, location_id),
                    )
                except (sqlite3.OperationalError, sqlite3.IntegrityError):
                    continue
        self.conn.execute("DELETE FROM locations WHERE id=?", (location_id,))
        self.conn.commit()

    def delete_location(self, location_id: int) -> None:
        """删除一个位置行及其附属数据（文件已被移走时用）。

        文件移进回收站后，位置行必须一起删——留着它，界面就会显示
        「文件还在但缺歌词」，下次扫描还会把它当成缺失文件重新登记。
        附属表都是 ON DELETE CASCADE，会跟着一起清掉。
        """
        self.conn.execute("DELETE FROM locations WHERE id=?", (location_id,))
        self.conn.commit()

    def _drop_duplicate_children(
        self, table: str, old_location_id: int, keep_location_id: int
    ) -> None:
        """删掉旧位置行里与存活行重复的附属记录。

        判定「重复」按各表的自然键：暂存歌词按 content_hash、标签提案
        按 location（每位置一条）、歌词观察按 (location, 观测时间)、
        其余按 location 唯一。
        """
        try:
            if table == "staged_lyrics":
                self.conn.execute(
                    "DELETE FROM staged_lyrics WHERE location_id=? AND content_hash IN "
                    "(SELECT content_hash FROM staged_lyrics WHERE location_id=?)",
                    (old_location_id, keep_location_id),
                )
            elif table == "tag_proposals":
                self.conn.execute(
                    "DELETE FROM tag_proposals WHERE location_id=?",
                    (old_location_id,),
                )
            elif table == "lyric_observations":
                self.conn.execute(
                    "DELETE FROM lyric_observations WHERE location_id=? AND id NOT IN "
                    "(SELECT MAX(id) FROM lyric_observations "
                    " WHERE location_id=? GROUP BY embedded_state, sidecar_state)",
                    (old_location_id, keep_location_id),
                )
            else:
                self.conn.execute(
                    f"DELETE FROM {table} WHERE location_id=?", (old_location_id,)
                )
        except sqlite3.OperationalError:
            pass

    # -- 曲库整理：音频索引 / 重复组 / 操作日志 ---------------------------

    def cached_audio_index(
        self, root_id: int, rel_path: str, *, file_size: int, file_mtime: int
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM audio_index WHERE root_id=? AND rel_path=? "
            "AND file_size=? AND file_mtime=?",
            (root_id, rel_path, int(file_size), int(file_mtime)),
        ).fetchone()

    def upsert_audio_index(
        self,
        *,
        root_id: int,
        rel_path: str,
        file_size: int,
        file_mtime: int,
        media_format: str,
        audio_hash: str,
        duration: float | None,
        title: str,
        artist: str,
        album: str,
        recording_id: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audio_index(root_id, rel_path, file_size, file_mtime, "
            "media_format, audio_hash, duration, title, artist, album, recording_id, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(root_id, rel_path) DO UPDATE SET "
            "file_size=excluded.file_size, file_mtime=excluded.file_mtime, "
            "media_format=excluded.media_format, audio_hash=excluded.audio_hash, "
            "duration=excluded.duration, title=excluded.title, artist=excluded.artist, "
            "album=excluded.album, recording_id=excluded.recording_id, "
            "updated_at=excluded.updated_at",
            (
                root_id, rel_path, int(file_size), int(file_mtime), media_format,
                audio_hash, duration, title, artist, album, recording_id, time.time(),
            ),
        )

    def prune_audio_index(self, root_id: int, keep_paths: set[str]) -> int:
        """删除本次扫描不存在的旧索引行，避免改名后留下过期副本。"""
        rows = self.conn.execute(
            "SELECT rel_path FROM audio_index WHERE root_id=?", (root_id,)
        ).fetchall()
        stale = [str(row["rel_path"]) for row in rows if str(row["rel_path"]) not in keep_paths]
        if stale:
            self.conn.executemany(
                "DELETE FROM audio_index WHERE root_id=? AND rel_path=?",
                [(root_id, path) for path in stale],
            )
        return len(stale)

    def replace_dedupe_groups(
        self, root_id: int, groups: Sequence[dict[str, Any]]
    ) -> None:
        """用本次扫描结果整体替换该根的重复组。

        为什么整体替换而不是增量合并：组是「当前磁盘事实」的投影。
        文件被删除或改名后，旧组继续留着只会让用户点进一个不存在的文件。

        用户手动忽略过的组要留在这个状态里：按 group_key（分类 + 组内文件
        集合的指纹）把 dismissed_at 带过去。文件集合变了 group_key 就变，
        那一组会重新出现、重新判断。
        """
        now = time.time()
        dismissed = {
            str(row["group_key"]): row["dismissed_at"]
            for row in self.conn.execute(
                "SELECT group_key, dismissed_at FROM dedupe_groups "
                "WHERE root_id=? AND dismissed_at IS NOT NULL",
                (root_id,),
            )
        }
        with self.transaction() as tx:
            tx.execute("DELETE FROM dedupe_groups WHERE root_id=?", (root_id,))
            for group in groups:
                tx.execute(
                    "INSERT INTO dedupe_groups(root_id, group_key, kind, keep_rel_path, "
                    "keep_reason, items_json, found_at, dismissed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        root_id,
                        str(group["group_key"]),
                        str(group["kind"]),
                        str(group["keep_rel_path"]),
                        str(group["keep_reason"]),
                        json.dumps(group["items"], ensure_ascii=False),
                        now,
                        dismissed.get(str(group["group_key"])),
                    ),
                )
        self.conn.commit()

    def iter_dedupe_groups(
        self, *, include_dismissed: bool = False
    ) -> Iterator[sqlite3.Row]:
        sql = (
            "SELECT g.*, r.name AS root_name FROM dedupe_groups g "
            "JOIN roots r ON r.id=g.root_id"
        )
        if not include_dismissed:
            sql += " WHERE g.dismissed_at IS NULL"
        sql += " ORDER BY g.found_at DESC, g.id"
        yield from self.conn.execute(sql)

    def dedupe_group(self, group_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT g.*, r.name AS root_name FROM dedupe_groups g "
            "JOIN roots r ON r.id=g.root_id WHERE g.id=?",
            (group_id,),
        ).fetchone()

    def update_dedupe_group(
        self,
        group_id: int,
        *,
        keep_rel_path: str,
        keep_reason: str,
        items: Sequence[dict[str, Any]],
    ) -> None:
        self.conn.execute(
            "UPDATE dedupe_groups SET keep_rel_path=?, keep_reason=?, items_json=? "
            "WHERE id=?",
            (keep_rel_path, keep_reason, json.dumps(items, ensure_ascii=False), group_id),
        )
        self.conn.commit()

    def dismiss_dedupe_group(self, group_id: int) -> bool:
        """把一组标记为已忽略。

        之前这里是直接删行，下次扫描整体重建时又原样回来，等于没忽略。
        现在记一个 dismissed_at，扫描重建时按 group_key 带过去。
        """
        cur = self.conn.execute(
            "UPDATE dedupe_groups SET dismissed_at=? WHERE id=? AND dismissed_at IS NULL",
            (time.time(), group_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def restore_dedupe_group(self, group_id: int) -> bool:
        """取消忽略，这一组重新回到待处理列表。"""
        cur = self.conn.execute(
            "UPDATE dedupe_groups SET dismissed_at=NULL WHERE id=? AND dismissed_at IS NOT NULL",
            (group_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def save_organize_plan(
        self, *, plan_id: str, root_id: int, subpath: str, payload: dict[str, Any]
    ) -> None:
        # 只保留最近 20 份未执行的计划，避免预览反复刷新把库撑大
        self.conn.execute(
            "DELETE FROM organize_plans WHERE consumed_at IS NULL AND created_at < ?",
            (time.time() - 6 * 3600,),
        )
        self.conn.execute(
            "DELETE FROM organize_plans WHERE consumed_at IS NULL AND id NOT IN ("
            "SELECT id FROM organize_plans WHERE consumed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 20)"
        )
        self.conn.execute(
            "INSERT INTO organize_plans(id, root_id, subpath, payload_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (plan_id, root_id, subpath, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def organize_plan(self, plan_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM organize_plans WHERE id=?", (plan_id,)
        ).fetchone()

    def consume_organize_plan(self, plan_id: str) -> None:
        self.conn.execute(
            "UPDATE organize_plans SET consumed_at=? WHERE id=?", (time.time(), plan_id)
        )

    def record_organize_op(
        self,
        *,
        root_id: int,
        batch_id: str,
        op_kind: str,
        old_rel_path: str,
        new_rel_path: str | None = None,
        trash_rel_path: str | None = None,
        detail: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO organize_ops(root_id, batch_id, op_kind, old_rel_path, "
            "new_rel_path, trash_rel_path, detail, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                root_id, batch_id, op_kind, old_rel_path, new_rel_path,
                trash_rel_path, detail, time.time(),
            ),
        )
        return int(cur.lastrowid or 0)

    def organize_ops(self, *, limit: int = 500, active_only: bool = True) -> list[sqlite3.Row]:
        sql = (
            "SELECT o.*, r.name AS root_name FROM organize_ops o "
            "JOIN roots r ON r.id=o.root_id"
        )
        if active_only:
            sql += " WHERE o.undone_at IS NULL"
        sql += " ORDER BY o.created_at DESC, o.id DESC LIMIT ?"
        return list(self.conn.execute(sql, (int(limit),)))

    def organize_op(self, op_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT o.*, r.name AS root_name FROM organize_ops o "
            "JOIN roots r ON r.id=o.root_id WHERE o.id=?",
            (op_id,),
        ).fetchone()

    def mark_organize_undone(self, op_id: int) -> None:
        self.conn.execute(
            "UPDATE organize_ops SET undone_at=? WHERE id=?", (time.time(), op_id)
        )

    # -- 规则 -------------------------------------------------------------

    def add_rule(
        self,
        *,
        scope: str,
        target: str,
        action: str,
        reason: str | None = None,
        expires_at: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO rules(scope, target, action, reason, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(scope, target, action) DO UPDATE SET "
            "reason=excluded.reason, expires_at=excluded.expires_at",
            (scope, target, action, reason, time.time(), expires_at),
        )
        return int(cur.lastrowid or 0)

    def iter_rules(self, *, now: float | None = None) -> Iterator[sqlite3.Row]:
        now = now if now is not None else time.time()
        yield from self.conn.execute(
            "SELECT * FROM rules WHERE expires_at IS NULL OR expires_at>? ORDER BY scope, target",
            (now,),
        )

    def remove_rule(self, rule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        return bool(cur.rowcount)

    # -- 鉴权 -------------------------------------------------------------

    def get_user(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM auth_users WHERE username=?", (username,)
        ).fetchone()

    def upsert_user(self, *, username: str, password_hash: str) -> int:
        now = time.time()
        with self.transaction() as tx:
            row = tx.execute(
                "SELECT id FROM auth_users WHERE username=?", (username,)
            ).fetchone()
            if row:
                tx.execute(
                    "UPDATE auth_users SET password_hash=?, updated_at=? WHERE id=?",
                    (password_hash, now, row["id"]),
                )
                return int(row["id"])
            cur = tx.execute(
                "INSERT INTO auth_users(username, password_hash, created_at, updated_at) "
                "VALUES(?,?,?,?)",
                (username, password_hash, now, now),
            )
            return int(cur.lastrowid or 0)

    def user_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM auth_users").fetchone()
        return int(row["n"]) if row else 0

    def add_token(
        self, *, token_hash: str, label: str | None, expires_at: float | None
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO auth_tokens(token_hash, label, created_at, expires_at) "
            "VALUES(?,?,?,?)",
            (token_hash, label, time.time(), expires_at),
        )
        return int(cur.lastrowid or 0)

    def find_token(self, token_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM auth_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()

    def touch_token(self, token_id: int) -> None:
        self.conn.execute(
            "UPDATE auth_tokens SET last_used_at=? WHERE id=?", (time.time(), token_id)
        )

    def revoke_token(self, token_id: int) -> None:
        self.conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE id=?", (time.time(), token_id)
        )

    def iter_tokens(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM auth_tokens WHERE revoked_at IS NULL ORDER BY created_at DESC"
        )

    # -- 维护 -------------------------------------------------------------

    def integrity_check(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def table_counts(self) -> dict[str, int]:
        names = [
            "roots", "assets", "locations", "metadata_revisions", "lyric_observations",
            "rules", "jobs", "resolutions", "approvals", "transactions", "sidecar_writes",
        ]
        out: dict[str, int] = {}
        for name in names:
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()  # noqa: S608
            out[name] = int(row["n"]) if row else 0
        return out


def open_store(data_dir: str | Path, *, migrate: bool = True) -> StateStore:
    """打开（必要时创建）状态库。"""
    data_dir = Path(data_dir)
    store = StateStore(path=data_dir / "lyra.db")
    store.connect()
    if migrate:
        store.migrate()
    return store


def load_runtime_roots(store: StateStore) -> list[dict[str, Any]]:
    """读取**运行时新增**的受管根（设置页添加的，不在配置文件里的）。

    配置文件里的根由 `sync_root_profiles` 登记；这里取的是用户在设置页
    新增的那些——它们只存在于状态库里，重启后仍然有效。
    """
    rows = store.conn.execute(
        "SELECT name, role, path, host_path, display_name, read_only, enabled "
        "FROM roots WHERE runtime = 1 ORDER BY name"
    ).fetchall()
    return [dict(row) for row in rows]


def runtime_roots_as_configs(store: StateStore) -> list[Any]:
    """把运行时新增的根转成 RootConfig（供合并进 cfg.roots）。"""
    from .config import RootConfig, RootRole

    out: list[Any] = []
    for row in load_runtime_roots(store):
        try:
            role = RootRole(str(row.get("role") or "library"))
        except ValueError:
            role = RootRole.LIBRARY
        out.append(
            RootConfig(
                name=str(row["name"]),
                role=role,
                path=str(row["path"]),
                host_path=str(row.get("host_path") or ""),
                display_name=str(row.get("display_name") or ""),
                read_only=bool(row.get("read_only")),
                enabled=bool(row.get("enabled")),
            )
        )
    return out


def sync_root_profiles(store: StateStore, roots: Sequence[Any]) -> None:
    """把配置里的受管根登记进状态库，并同步**宿主机路径**。

    为什么宿主机路径要进库：容器里挂载点是 `/library`，磁盘上真实目录
    是 `/vol2/1000/Music/Library`。界面要显示用户认的那个名字与位置，
    就得知道宿主机路径——它只在 `lyra.toml` 里，状态库里原本没有。

    显示名（`display_name`）是**用户设置**，这里不覆盖：只在它为空时
    用配置里的 `host_path` 补上，之后用户改了就以用户的为准。
    """
    for root_cfg in roots:
        role = getattr(root_cfg.role, "value", str(root_cfg.role))
        host_path = str(getattr(root_cfg, "host_path", "") or "")
        root_id = store.upsert_root(
            name=root_cfg.name,
            role=str(role),
            path=str(root_cfg.resolved_path),
            enabled=bool(root_cfg.enabled),
            read_only=bool(root_cfg.read_only),
            host_path=host_path,
        )
        if host_path:
            row = store.conn.execute(
                "SELECT host_path FROM roots WHERE id=?", (root_id,)
            ).fetchone()
            if row is not None and not str(row["host_path"] or ""):
                store.conn.execute(
                    "UPDATE roots SET host_path=? WHERE id=?",
                    (host_path, root_id),
                )
                store.conn.commit()


def resolve_ids_by_hashes(
    store: StateStore, hashes: Iterable[str]
) -> dict[str, int]:
    """按音频哈希批量取 asset id，供「同一录音的多个位置共享缓存」使用。"""
    out: dict[str, int] = {}
    for h in hashes:
        row = store.conn.execute(
            "SELECT id FROM assets WHERE audio_hash=?", (h,)
        ).fetchone()
        if row:
            out[h] = int(row["id"])
    return out
