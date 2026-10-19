# 架构

## 总览

```text
┌─────────────────────────────────────────────────────────────┐
│ Browser / CLI / Mobile                                      │
│  Vue 3 SPA        lyra CLI        HTTP + Bearer Token        │
└───────────────┬──────────────────────────────┬──────────────┘
                │ /api/v1                      │
        ┌───────▼────────┐              ┌──────▼───────┐
        │ lyra (FastAPI) │              │ lyra-watch   │
        │ API + static   │              │ watcher      │
        └───────┬────────┘              └──────┬───────┘
                │                              │
                └──────────────┬───────────────┘
                               ▼
                     SQLite WAL + data/
                     backups / trash / staging
                               │
                               ▼
                        Music library files
```

两个进程使用同一个镜像：

- `lyra serve`：FastAPI、前端静态资源、人工操作接口。
- `lyra watch`：文件监听、稳定判定、周期对账、每日完整核对、自动歌词/标签任务、重复检测。

写入通过数据库中的写租约串行化。人工操作优先：监控检测到有人在等待写租约时，会在当前文件处理完后让出。

## 代码地图

```text
src/lyra/
  api.py            FastAPI 路由、后台任务、静态资源
  cli.py            命令行入口
  config.py         配置解析、校验、密钥掩码
  db.py             SQLite schema、迁移、租约、事务
  models.py         领域枚举与数据结构
  scan.py           递归发现、忽略规则
  audit.py          只读审计、配对、问题码
  stats.py          统计聚合
  pipeline.py       抓取 → 匹配 → 暂存 → 写入
  match.py          候选评分、时长闸门、版本判断
  providers/        QQ、网易云、LRCLIB
  tagsources/       MusicBrainz 查询与字段比较
  tagscrape.py      标签匹配与提案
  tagwrite.py       标签写入与撤销
  writer.py         字节级写入器、备份、事务、恢复
  flacmeta.py       FLAC 元数据块
  id3meta.py        ID3v2 帧
  oggmeta.py        OGG 页与 comment
  formats.py        容器嗅探、统一读取
  fingerprint.py    Chromaprint / AcoustID
  organize.py       路径生成、移动、空目录、撤销
  dedupe.py         音频索引、分类、删除
  rename.py         改名与播放列表引用
  cover.py          封面下载与 SSRF 防护
  ai.py             可选的语义复核
  web/              前端构建产物（不入库）
web/                 Vue 3 + Vite 源码
tests/               pytest 与前端 vitest
docs/                文档
deploy/              高级部署示例
```

## 数据流

### 歌词

1. `scan` 发现音频和 sidecar。
2. `audit` 读取标签、内嵌歌词和 `.lrc`，生成问题码。
3. `pipeline.run_fetch` 选择候选来源，评分并写入暂存区。
4. 人工确认或自动策略决定是否写入。
5. `writer` 备份、写内嵌、写 `.lrc`、校验、提交事务。

### 标签

1. `tagscrape.scan_tags` 读取每首曲目的标签和指纹。
2. 按 MBID、目录共识、搜索、指纹的顺序找候选。
3. `compare_fields` 逐字段生成 `same` / `fill` / `correct` / `conflict`。
4. 自动策略写入安全字段；冲突留在待审查。
5. `tagwrite` 只改目标字段，记录原值，支持按字段撤销。

### 整理

1. `organize.build_plan` 读取标签，生成目标路径。
2. 计划落库，返回预览。
3. 人工确认后 `execute_plan` 校验计划、移动文件、记录批次。
4. 空目录清理单独预览和执行。
5. 撤销按 `organize_ops` 倒序恢复。

### 重复

1. `dedupe.scan_duplicates` 读取音频哈希、标签、录音 ID 和时长。
2. 先建精确组，再建同一录音组，最后建相似组。
3. 精确组和录音组的保留项作为后续分类的参照，不参与删除。
4. 分类结果落库；删除前校验所有组，再移入回收站。
5. 删除后重新扫描，避免留下不存在的参照。

## 数据库

Schema v12，主要表：

| 表 | 用途 |
| --- | --- |
| `roots` | 受管根 |
| `locations` | 音频位置、相对路径、缺失状态 |
| `lyric_observations` | 每次审计的歌词状态与问题 |
| `staged_lyrics` | 抓取后的歌词暂存 |
| `resolutions` | 匹配结果与原因 |
| `writes` / `write_fields` | 歌词/标签写入与字段原值 |
| `renames` | 改名记录 |
| `organize_ops` | 移动、删除、空目录清理 |
| `organize_plans` | 整理预览计划 |
| `audio_index` | 音频哈希、时长、标签、录音 ID |
| `dedupe_groups` | 重复分类结果 |
| `auth_users` / `auth_tokens` | 账户与令牌 |
| `meta` | 设置、心跳、迁移版本 |

迁移在跨进程文件锁内执行，版本号只增不减。迁移前自动备份。

## 写入安全

写入器不调用 `mutagen.save()`。它按容器格式做字节级改写：

- FLAC：重写 Vorbis comment 块，保留 padding、音频帧和其他块。
- MP3：解析 ID3v2 帧，只替换目标帧，保留未知帧和音频数据。
- OGG：重排 comment 页，保持音频页和 granule position。

写入流程：

1. 读取原文件，计算完整哈希和音频哈希。
2. 备份原文件（按策略）。
3. 写入临时文件。
4. 校验音频哈希、非目标标签、目标字段。
5. `fsync` 后原子替换。
6. 提交事务记录。

失败时删除临时文件并回滚。崩溃后根据实际文件哈希和备份材料恢复。

## 安全边界

- 所有 `root + path` 接口都经过 `_safe_root_file`，拒绝绝对路径、`..` 和符号链接逃逸。
- 封面下载只允许 http/https，解析出的每个地址都必须是公网地址。
- 密码只存 scrypt 哈希，Token 只存 SHA-256。
- 写接口套写租约，避免 Web 和 watch 同时改同一文件。
- 只读模式和 `read_only` 根在接口层拒绝写入。
- 反向代理需要传 `X-Forwarded-Proto`，否则 Cookie 的 Secure 判断会失效。

## 设计取舍

**为什么不用数据库存音频二进制。** 曲库是用户的文件，Lyra 只做就地维护。数据库只存索引、状态和审计记录，随时可以从磁盘重建。

**为什么把抓取和写入分开。** 抓取可以放心重试，写入才需要租约、备份和校验。分开后，人工审查可以插入中间。

**为什么重复分类要求互斥。** 传递闭包会把精确副本、同一录音和相似版本串成一个大组，导致一键删除跨分类误删。互斥分类加参照项，保证每次批量操作只影响一个证据等级。

**为什么删除进回收站。** 同卷 rename 是瞬时的，不占额外空间；误删可以恢复。永久删除必须由人明确操作。
