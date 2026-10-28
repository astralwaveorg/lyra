# Lyra 重构方案：交互重组 + 全自动入库流水线

> 状态：设计稿，未实施。本文档是重构的完整方案，覆盖信息架构、页面交互与自动化能力三条线。
> 依据：对当前代码（v1.3.0）的逐模块分析——前端 11 个视图、`api.py` 全部约 100 个端点、`pipeline.py` / `watcher.py` / `match.py` / `tagscrape.py` / `audit.py` / `cover.py` / `formats.py` 的实际能力与配置默认值。文中所有「现状」均对应真实代码行为，标注了模块名。

---

## 0. 结论速览

对用户提出的两个核心问题，直接给答案：

**问题一：「操作」与「审查」是一个页面分两个 tab，还是多个页面？**

答：**两个页面，但不是现在的两个页面。** 不做「一页两 tab」，也不维持现状的多页并列。推荐按导航模式拆分：

- **音乐**页承载「操作」（对象驱动：先找到专辑/曲目，再编辑）；
- **待办**页承载「审查」（队列驱动：先看待决策项，再定位对象）；
- 两个页面点开任何一个曲目，都落入**同一个详情抽屉**（歌词 / 封面 / 标签 / 文件四个模块 tab）——歌词、封面、标签三大模块以「详情抽屉的 tab」和「待办的队列分类」两种形态出现，而不是三组独立页面。

详细论证与三个备选方案的对比见 §3.1。

**问题二：自动化做到「任何歌曲扔进去就处理好」，怎么达成？**

答：补上当前自动化链路缺的三块——**身份解析层**（现在只有标签链路有，歌词链路靠裸标签查询，封面完全没有）、**封面自动化**（现状为零：无来源、MP3/OGG 不可写）、**自动归置**（现状为纯人工）。组成一条统一入库流水线：

```
扔进 INBOX（或任意受管根）→ 稳定判定 → 身份解析 → 并行补全（标签/歌词/封面）→ 归位 → 质检 → 待办只留真正需要人的
```

核心原则：**自动化按「证据强度」决定每个动作，而不是一个全局阈值**；所有自动动作可撤销、可追溯；人的精力只花在机器不确定的少数项上。详见 §4。

**交付节奏**：分四个阶段（§6），每阶段独立可发布：① 信息架构与体验 → ② 封面从 0 到 1 + 来源健壮性 → ③ 身份层与入库流水线 → ④ 自动归置。QQ/网易云非官方接口的风险采用三层防线（§4.10）：默认直连加固 + 可选自建网关 + 契约化解析与健康面板，不引入第三方 API 依赖。方案已细化到开发级（附录 A–G：迁移 DDL、API 契约、函数签名、前端规格、配置全表、测试矩阵、分阶段文件清单），全部开放问题已定案（§7 决策记录）。

---

## 1. 现状诊断

### 1.1 功能与页面盘点

现有 11 个页面（`web/src/router/index.ts`）：

| 页面 | 实际职能 | 规模 |
| --- | --- | --- |
| 概览 | 曲库健康仪表盘：8 张 KPI 卡 + 7 组图表 + 3 条待办跳转 | 423 行 |
| 音乐 | 曲目列表 + 筛选（问题码/歌词状态）+ 详情抽屉（标签/封面/歌词/文件四 tab）+ 单曲刮削弹窗 | 329 行 |
| 文件 | 全文件列表（含非音频）+ 预览抽屉 + 批量删除 | 362 行 |
| 歌词审查 | 四 tab：推荐采纳 / 待审查 / 已忽略 / 无音源（孤儿 LRC）；批量采用、逐行对比、来源切换 | 1270 行 |
| 歌词刮削 | 批量抓取任务发起（范围/筛选/立即写入）+ 任务历史 + 监控状态 | 579 行 |
| 标签刮削 | 提案队列（auto/review/unmatched/ignored 四个 tab；done 为数据层状态）+ 逐字段对照 + 链接指定 + 批量写入 + 历史 | 996 行 |
| 曲库整理 | 三 tab：一键整理（预览/执行/冲突/lrc 跟随）+ 重复音频（分类/批量删除/忽略）+ 空目录清理 | 1381 行 |
| 回收站 | 按批次还原 / 永久删除 / 清空 | 206 行 |
| 日志 | 四 tab：写入记录 / 改名 / 整理记录 / 事务；批量撤销 | 484 行 |
| 设置 | 八张卡片：账户/外观/标签策略/AI/代理/密码/Token/运行信息 | 980 行 |
| 关于 | 版本与赞助信息 | 289 行 |

### 1.2 信息架构问题（体验差的根因）

按影响排序，每条都对应可指认的现状：

1. **同一件事被拆成三页。** 「给歌补歌词」的完整动作链是：审查队列确认（歌词审查页）→ 不满意就手动刮削（弹出 ScrapeDialog，这个弹窗同时挂在审查页和音乐页两处）→ 批量补抓要去歌词刮削页 → 结果又回到审查页。用户在三个页面间往返，而它们操作的是同一个对象的同一种属性。
2. **标签同理。** 标签刮削页是队列，音乐页的详情抽屉里还有一套独立的标签编辑器（TagEditorPanel）和 MB 补全（填草稿式），加上链接指定弹窗（TagLinkDialog），**字段对照 UI 一共三套**，交互各不相同（一套填草稿、一套换源、一套直接写入）。
3. **「审查确认时缺上下文」。** 审查页只显示候选与分数，确认前想看封面、想看标签对不对、想试听，要跳去音乐页重新找到这首歌。审查是「对对象的决策」，决策需要对象的全量信息，现在的架构把两者分开了。
4. **封面 buried 最深。** 补一张封面的路径：音乐页 → 行内「详情」→ 抽屉切「封面」tab → 四选一动作。没有专辑级批量（同一专辑 15 首要嵌 15 次），没有网络搜索（只能手动贴 URL），且**封面写入只支持 FLAC**（`writer.apply_cover_edit` 硬编码 `MediaFormat.FLAC`），MP3/OGG/Opus 点写入直接 422。
5. **歌词不能手改。** 详情抽屉的歌词 tab 只读（LyricsViewer），全站没有歌词文本编辑组件。歌词不对时只能「换一条候选」或「替换整个文件」，不能手动修一行、调一个时间轴。
6. **整理页三合一过载。** 1381 行，移动、查重、空目录三个业务塞一页，查重部分有 5 种状态 × 筛选 × 默认勾选的复杂状态机，首次使用很难建立心智模型。
7. **撤销路径分散。** 写入撤销在日志页、整理撤销在日志页的整理 tab、删除还原在回收站页、歌词忽略恢复在审查页、标签忽略恢复在标签页——五种「反悔」五种入口，且互相没有链接（整理页说「可在整理日志批量撤销」但不给链接）。
8. **待办跳转断链。** 概览「待处理」三行，点「标签与封面问题」跳 `/library` 但**不带任何筛选参数**，用户落地后要自己再找筛选器重新构造同一个视图。
9. **概念与文案不统一。** 「刮削/写入/采用/填入/落盘」五种动词指同一类事；「删除」按钮实际是「移入回收站」；「事务/恢复冲突」是工程术语直接暴露给用户；删除类确认文案分散在四处（回收站销毁、文件批量删除、查重批量删除、空目录清理）措辞不一。
10. **空状态无出路。** 各页 EmptyState 基本只有一句话，没有「接下来该做什么」的动作按钮。

### 1.3 自动化能力现状（能力矩阵）

watch 进程（`watcher.py` + `cli.py` 主循环）当前自动做的事：

| 能力 | 新文件触发 | 周期任务 | 人工入口 | 缺口 |
| --- | --- | --- | --- | --- |
| 内嵌→.lrc 生成 | ✅ `fill_sidecars` | ✅ 随 rescan | — | 只有单方向；受 `fill_empty_lrc` 控制 |
| 歌词抓取 | ✅ `run_fetch` | ✅ 30s 对账补漏 | 刮削页批量 / 单曲弹窗 | 匹配质量依赖标签；翻译丢弃；重试耗尽后无出口 |
| 歌词自动写入 | ✅ `run_apply`（仅 accept 决策） | ✅ | 审查页采用 | needs_review 队列只进不出（人工） |
| 标签扫描 | ❌ | ✅ 默认 24h | 标签页手动 | unmatched 无智能出口 |
| 标签自动写入 | ❌ | ✅ 只补缺失字段 | — | 冲突留人（合理，保留） |
| 封面 | 仅问题码探测 | 文件名规范化（1h） | 详情抽屉 4 路 | **无任何抓取来源、无自动嵌入、MP3/OGG/Opus 不可写** |
| 目录归置 | ❌ | ❌ | 整理页预览+执行 | **纯人工；新文件也不归位**；`RootRole.INBOX` 只是预留枚举（`models.py:31`） |
| 重复检测 | ❌ | ✅ 24h | 整理页 | 只检测不删除（红线，保留） |
| 孤儿 LRC | ❌ | 默认关（`auto_clean_orphan_sidecars=false`） | 审查页 | AI 判重已有范式 |
| Sidecar 名修复 | — | ✅ 5min | — | — |

结论：**歌词链路自动化骨架良好（负缓存、退避重试、租约、熔断都在），但「身份解析」只存在于标签链路；封面与归置的自动化为零。** 这正对着用户的目标：扔进去 → 归位 + 标签 + 封面 + 歌词，四个环节里两个是空白。

### 1.4 准确性短板

1. **歌词匹配的输入是「脏标签」。** `pipeline.run_fetch` 直接用文件标签构造 `TrackQuery`（`providers/base.py`），标签错了（写歌名把 Live 版写成录音室版、艺人写反）歌词就跟着错。而标签链路明明有 MBID/目录锚点/指纹这套身份证据，两条链路不互通。
2. **版本闸门有但输入受限。** `match.py` 的版本词表（live/remix/instrumental…）只比对标签与候选标题；文件名里的版本标记没有参与。
3. **指纹兜底默认关闭。** `providers.acoustid.enabled=false`，多数部署永远不会用上，低置信场景全部堆进待审查。
4. **翻译歌词抓了但从未落盘。** `RawLyrics.translation` 字段存在（QQ/网易云都产出），但 `pipeline.py` / `writer.py` 中 `translation` 零引用——数据在源头就被丢弃。
5. **逐字歌词（QRC/KRC）降级丢弃。** 只保留行级 LRC + `word_level=True` 布尔标记。
6. **needs_review 无消化机制。** 队列只靠人来清，AI 判重（`ai.py`）只用于孤儿 LRC 一个场景，没有推广到歌词候选预审。
7. **重试耗尽静默。** `not_found` 三轮（7/30/90 天）用尽后停等，没有汇总报告，用户不知道哪些歌永远缺歌词。

---

## 2. 产品模型重构

### 2.1 三层模型：身份 → 内容 → 归置

把系统重新表述为三层，这是整个重构的骨架：

```
┌─ 归置层 ────────────────────────────────────────┐
│  身份确定后，文件放在哪里（艺人/专辑/音轨号-曲名）  │
├─ 内容层 ────────────────────────────────────────┤
│  身份确定后，补什么（标签字段 / 歌词 / 封面）        │
├─ 身份层 ────────────────────────────────────────┤
│  这份文件是哪首歌（录音）/ 哪张专辑（发行）          │
│  证据：文件自证 MBID·ISRC > 音频指纹 > 目录共识      │
│        > 标签+文件名搜索 > AI 语义判断              │
└─────────────────────────────────────────────────┘
```

现状的问题本质：身份解析只在标签链路存在（`tagscrape.py` 的 MBID→锚点→搜索→指纹四级），歌词链路独立打分，封面链路没有身份概念，归置链路只在人工执行时读标签。重构后**一次身份解析，驱动三种补全 + 一次归位**，四条线共享同一份证据与置信度。

### 2.2 证据分级与信任档位

**证据等级**（E 级，供全系统引用）：

| 等级 | 含义 | 来源 |
| --- | --- | --- |
| E0 | 文件自证 | 标签内的 MUSICBRAINZ_*MBID / ISRC |
| E1 | 音频指纹 | AcoustID（chromaprint）→ MB 录音 |
| E2 | 目录共识 | 可信目录锚点（`tagscrape._folder_anchor`，coverage/name_sim 判定已有）+ 多文件一致 |
| E3 | 单文件吻合 | 标题+艺人搜索、时长闸门、版本一致性 |
| E4 | AI 语义判断 | 文件名/歌词内容/元数据的语义比对（复用 `ai.py` 范式：置信度 + 预算 + 程序做最终决策） |

**自动化档位**（每模块可独立覆盖）：

| 档位 | 行为 | 对应现状 |
| --- | --- | --- |
| L0 只读 | 一切只暂存、出报告，不写文件 | 对应 `policy.write_enabled=false`（`write_enabled=true` + `write_mode=off` 的组合本就不合法） |
| L1 稳健（默认） | 补空与高置信匹配自动写入；纠正/冲突/低证据进待办 | 对应现状 `write_mode=auto` 的行为面 |
| L2 进取 | 高证据（E0/E1）纠正自动写；网络封面自动嵌入；新文件自动归位 | 新增 |
| L3 全自动 | 冲突按强证据自动纠正；AI 预审消化待办队列；全部归位 | 新增 |

档位与现状的兼容映射：升级部署 `write_mode=auto → L1`（行为不变）、`write_enabled=false → L0`；`verified_only` 是比 `auto` 更严的既有档位，映射 L1 并保留其来源过滤语义；新装默认 L1。`write_mode` 键保留并继续生效，档位是其上层抽象。

**动作 × 证据 矩阵**（L1 档默认值示例，完整矩阵进配置文档）：

| 动作 | E0 | E1 | E2 | E3 | E4 |
| --- | --- | --- | --- | --- | --- |
| 补缺失标签字段 | auto | auto | auto | auto（核验通过来源）/ review（未核验） | never |
| 纠正标签字段（发行级） | review | review | auto（可信目录，现状行为） | review | never |
| 写歌词（内嵌+lrc） | auto | auto | auto（高置信） | auto（高置信）/ review（其余） | AI 高置信自动（留痕可撤销）/ review |
| 嵌入同目录封面图 | auto | — | auto | — | — |
| 抓取网络封面并嵌入 | review（L2 起 auto） | — | review | review | never |
| 移动文件归位 | auto（仅新文件 + 显式开启） | — | review（出建议不出手） | review | never |
| 删除任何文件 | **永不自动**（红线） | 同 | 同 | 同 | 同 |

**档位 × 模块行为对照**（「扔进去就处理好」需要开到哪一档，一目了然）：

| 模块 | L0 只读 | L1 稳健（默认） | L2 进取 | L3 全自动 |
| --- | --- | --- | --- | --- |
| 歌词 | 只抓取暂存，不写 | 高置信自动写内嵌 + `lrc`；其余进待办 | 同 L1 | + AI 预审建议，低风险项可批量确认 |
| 标签 | 只出提案 | 补空自动写；冲突进待办 | + 高证据纠正自动写 | + 强证据冲突自动纠正 |
| 封面 | 只报告缺失 | 同目录图自动嵌入；网络抓取进待办确认 | + 网络封面自动嵌入 | 同 L2 |
| 归置 | 只出归置建议 | 同 L0（默认不动） | + 新文件自动归位 | 同 L2（存量仍人工） |
| 人工介入点 | 全部 | 冲突 / 低证据 / 网络封面确认 | 冲突 / 存量归置 | 强证据之外的极少数 |

### 2.3 红线盘点：哪些保留、哪些需要改

**保留不动**（与现有代码注释和 roadmap 一致）：

- 禁止 `mutagen.save()`，写入走字节级改写 + 音频哈希校验 + 失败回滚（`writer.py`）；
- 路径先过 `_safe_root_file` / `_resolve_within_root`，目标存在绝不覆盖；
- 新写接口套 `@manual_lease()`，人工优先让出写租约；
- 删除永不自动，删除即回收站，可撤销；
- 人工痕迹保护：`tag_proposals` 的 ignored/done/source=link 不被自动扫描覆盖，写入完成后标 done 不删行（`db.upsert_tag_proposal`）；
- 歌词归属判定只认「同一首」（`audit.pair_sidecars` 五级配对、占位文案不算已有歌词）；
- 移动/删除先落库预览，记录批次；
- 重复分类互斥、参照项不参与删除。

**需要显式修改的一条红线**（已定案，见 §7 决策记录 1）：

- 现状「不在后台自动移动文件」是 roadmap 明文红线。但用户目标要求「自动处理好位置归属」。**方案：把红线从「永不移动」改为「默认不动 + 显式开启 + 仅限新入库文件 + 全程可撤销」**——归置自动化只作用于两条受控路径：INBOX 根的入库流程、以及 `policy.organize.mode=new_files` 显式开启的受管根；存量曲库的批量移动永远走整理页的人工预览+执行（现状保留）。所有移动照旧记 `organize_ops` 可逐条撤销，冲突（目标存在、字段缺失降级仍歧义）一律转待办不动手。

---

## 3. 信息架构与页面重构

### 3.1 核心决策：「操作」与「审查」怎么组织

三个备选方案：

**方案 A：一个页面，操作/审查两个大 tab。**
- 优点：入口唯一，侧栏最简。
- 缺点：两种任务的工作方式相反——操作是「搜索/筛选 → 定位对象 → 编辑」（需要常驻的列表+筛选器），审查是「队列 → 逐项决策」（需要大信息密度的队列+批量工具栏）。塞进一个页面后，tab 承担了页面级信息架构，切 tab 时筛选器、批量勾选、滚动位置全部互相干扰；窄屏下 tab 套 tab 无法使用。且歌词/标签/封面三类审查的行渲染完全不同，一个 tab 容器里会出现三种完全不同的列表，认知负担反而高。

**方案 B：维持多页并列（现状）。**
- 缺点即 §1.2 列出的十条：同一流水线的上下游分家、字段对照三套 UI、审查缺上下文、封面 buried、撤销五处。

**方案 C（推荐）：按导航模式拆成两个页面，按对象复用一套详情。**

```
        ┌────────────── 操作（对象驱动）──────────────┐
        │  音乐页：专辑网格 → 专辑详情 → 曲目行          │
        │           ↓ 点开任何曲目/专辑                 │
        │  详情抽屉（唯一编辑面）：歌词 | 封面 | 标签 | 文件 │
        └──────────────────┬───────────────────┘
                           │ 同一个抽屉组件
        ┌──────────────────┴───────────────────┐
        │  待办页：统一收件箱（队列驱动）            │
        │  歌词 | 标签 | 封面 | 归置 | 孤儿 | 失败   │
        │  行点击 → 打开同一个详情抽屉 → 决策        │
        └──────────────────────────────────────┘
```

- **歌词/封面/标签三大模块**存在于两个位置：详情抽屉的三个 tab（对单个对象操作），待办页的三个队列分类（跨对象审查）。不再是三组页面。
- 现有「歌词刮削/标签刮削」页的**批量任务发起**职能并入新的「自动化」控制台（任务表单 + 任务历史 + 全局策略），因为发起抓取和审查结果是同一条流水线的上下游，分开两页只制造跳转。
- 审查确认时缺上下文的问题被抽屉复用解决：待办里点开歌词候选，同一个抽屉里能看到标签对不对、封面有没有、能试听——全部上下文就位再决策。

**为什么不推荐 A**：A 看似简洁，实际把两种导航模式压进一个页面。数据佐证：现有审查页 1270 行、整理页 1381 行，已经是「一页多业务」过载的实证；方案 A 会把这种过载从页面间搬到 tab 间，更难维护。

**页面清单（新）**：

| 新页面 | 承接现状 | 职能 |
| --- | --- | --- |
| 概览 | 概览 | 健康度仪表盘 + 待办直达（跳转带筛选参数） |
| **音乐** | 音乐 + 文件 | 专辑为主视图的曲库浏览 + 全部编辑操作 |
| **待办** | 歌词审查 + 标签刮削（队列部分）+ 隐性队列 | 统一收件箱：六类队列 |
| **自动化** | 歌词刮削 + 标签刮削（任务部分）+ 设置的策略卡 | 流水线控制台：策略、任务、历史 |
| 整理 | 曲库整理（一键整理 + 空目录） | 目录归属预览与执行 |
| 查重 | 曲库整理（重复音频 tab） | 独立成页，降低整理页密度 |
| 回收站 | 回收站 | 补批量还原、搜索、筛选 |
| 活动 | 日志 | 统一撤销中心：写入/改名/整理/事务 |
| 设置 | 设置 | 分组导航 + 新策略项 |
| 关于 | 关于 | 不变 |

侧栏「维护」组保持 6 项但每项语义单一：待办（高频）> 自动化 > 整理 > 查重 > 回收站 > 活动。

### 3.2 路由迁移映射（保证无功能丢失）

| 旧地址 | 新去向 |
| --- | --- |
| `/review` | `/todo?kind=lyrics`（四个子 tab 映射到队列过滤） |
| `/scrape` | `/automation?tab=lyrics-tasks` |
| `/tags` | `/todo?kind=tags`（队列承接；批量任务入口移至自动化页标签任务区） |
| `/organize` | `/organize`（保留，去掉查重 tab） |
| `/library` `/files` | `/music`（文件视图并入音乐页的类型筛选） |
| `/logs` | `/activity` |
| 其余 | 不变 |

旧路由全部做客户端重定向保留（vue-router redirect，现有 `/orphans → /review?tab=orphans` 已是先例）。

### 3.3 页面详设

#### 3.3.1 音乐页（操作主场）

```
┌──────────────────────────────────────────────────────────┐
│ 筛选条：搜索 | 视图(专辑▦|曲目☰|文件) | 问题筛选▾ | 歌词状态▾ │
├──────────────────────────────────────────────────────────┤
│ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐   专辑网格视图（默认）      │
│ │封面  │ │封面  │ │封面  │ │封面  │   角标：缺封面/缺歌词/     │
│ │专辑名│ │专辑名│ │专辑名│ │专辑名│        待审查 数量徽标     │
│ │艺人  │ │艺人  │ │艺人  │ │艺人  │                         │
│ └─────┘ └─────┘ └─────┘ └─────┘                         │
└──────────────────────────────────────────────────────────┘
```

- **三种视图**：专辑网格（默认，因为封面/标签的发行级字段、目录归属都是专辑概念）、曲目列表（兼容现 LibraryView 的全部列与筛选）、文件列表（承接 FilesView，加「类型」筛选）。
- **专辑详情**（点专辑卡）：左侧专辑封面大图 + 发行级标签摘要；右侧曲目表（每行：音轨号、曲名、时长、歌词状态 tag、封面状态 tag、问题 chip、试听）。专辑级操作：**批量补封面**（一次下载，逐文件嵌入）、**批量刮削歌词**、**按 MB 发行修正全专辑标签**。
- **曲目详情抽屉**（点曲目行，全站唯一编辑面）四 tab：
  - **歌词**（重构）：状态摘要（内嵌/`lrc`/双语/来源）+ **可编辑**（新增歌词编辑器组件，见 §3.4）+ 候选列表（沿用 ScrapeDialog 的能力，但收进 tab 内，不再弹窗叠抽屉）+ 现有歌词与候选的 diff 对比（复用 AlternativesDrawer 的双栏 diff）+ 「改为同名」等 sidecar 操作保留。
  - **封面**（重构）：来源四路不变（上传/同目录/URL/移除）+ **新增「搜索封面」**（按专辑身份搜 CAA/网络来源，见 §4.4）+ 同目录候选图显示**缩略图**（现在只有文件名盲选）+ 显示格式支持提示（OGG/Opus 不可写时置灰并说明）。
  - **标签**（重构）：统一字段对照组件（§3.4），本地值/来源值/判断三列 + 逐字段勾选 + 一次「写入」完成（替代现在 MB 补全「查询→填入→写入」三步）。
  - **文件**：保留 FileOpsPanel（改名/替换/删除）+ 新增「重新入库」（对存量文件单发一次完整 ingest 流水线，§4.1）。
- **问题筛选**保留并扩充：问题码下拉带计数（现状已有）+ **从概览/待办跳入时自动带参**（修复 §1.2 第 8 条）。
- 音乐页**不设**批量歌词/标签审查入口——那属于待办页；音乐页的批量只做「按当前筛选发起任务」（跳自动化页预填范围）。

#### 3.3.2 待办页（审查主场，统一收件箱）

```
┌──────────────────────────────────────────────────────────────┐
│ 队列 tab： 歌词(12) | 标签(8) | 封面(3) | 归置(5) | 孤儿(2) | 失败(1) │
├──────────────────────────────────────────────────────────────┤
│ 分组头（按建议分组）：建议直接采纳 (9)   [全选] [采用所选(9)]        │
│ ┌──────────────────────────────────────────────────────┐     │
│ │☐ 曲目名 · 专辑 · 试听▶   来源badge 分数 时长差           │     │
│ │  闸门摘要（哪些检查通过/未过）   [查看全文] [采用] [忽略]    │     │
│ └──────────────────────────────────────────────────────┘     │
│ 需要逐首判断 (3) · 无建议 (0) …                                 │
│ 底部：选中项的批量工具栏（采用/忽略/驳回/指定来源）                  │
└──────────────────────────────────────────────────────────────┘
```

六类队列的内容与来源：

| 队列 | 内容 | 承接现状 |
| --- | --- | --- |
| 歌词 | 推荐采纳（safe）/ 待审查（needs_review）/ 已忽略规则 | 歌词审查页四 tab 原样迁入，批量与对比能力不变 |
| 标签 | auto 待写 / review 冲突 / unmatched / ignored，另增 done 过滤（现为数据层状态，新队列页将其作为可筛选视图） | 标签刮削页队列迁入，逐字段对照、链接指定、批量写入不变 |
| 封面 | 缺封面（按专辑聚合，一行=一张专辑）+ 待确认的网络封面候选 | 新增（现状只是库筛选器） |
| 归置 | 归位建议（源路径→目标路径 diff）、归位冲突、字段缺失降级 | 新增（现状整理页只有主动预览） |
| 孤儿 | 无音源 LRC（AI 判重 / 「设为这首的歌词」指定归属 / 清理） | 歌词审查页 orphans tab 迁入，三个行动作与 AI 判重不变 |
| 失败 | 写入失败、重试耗尽（歌词 not_found 3 轮用尽）、来源下架、事务恢复冲突 | 新增汇总（现状散落各处或静默） |

统一交互规范：

- 每行渲染按队列类型定制，但**外壳一致**：勾选框 + 主信息 + 证据摘要 + 主操作（采纳/应用）+ 次操作（查看/忽略/指定）。
- **行内默认勾选逻辑保留现状的成熟经验**：歌词「推荐采纳」默认全选建议项、孤儿 LRC 默认勾「与已有歌词重复」（`OrphansPanel` 现行为）、标签冲突字段默认勾选——「机器给出默认勾选，人只改不对的」。
- 点开行 → 详情抽屉（同一个）；抽屉内完成决策后队列实时刷新。
- **排序按风险**：建议直接采纳在最上（可一次批量清掉），需要逐首判断其次，无建议垫底。
- 「已忽略」不再单独占 tab（现状歌词审查的做法），降为各队列内的过滤视图；单条「恢复」保留在队列行内（迁移现状交互），批量规则管理集中在自动化页（§3.3.3）——两者操作同一份规则数据，不出现两套状态。
- 计数徽标沿用侧栏机制（现状 `needsReview` 徽标扩展为总待办数）。

#### 3.3.3 自动化页（控制台）

三个区块：

1. **策略**（承接并扩展设置页「标签刮削」卡 + 新增模块策略）：
   - 总开关：写入开关（现状 `policy.write_enabled`，保留）；
   - 档位选择：L0/L1/L2/L3（§2.2），每模块（歌词/标签/封面/归置）可覆盖；
   - 歌词策略：翻译落盘方式（off / 双语文体 / 独立字段）、逐字歌词保留开关、无结果重试计划（`retry.*_intervals_days` 可调）、孤儿自动清理（现状 `auto_clean_orphan_sidecars`）；
   - 封面策略：同目录图自动嵌入开关、网络抓取来源开关（CAA/发行商站点）、最小尺寸、自动级别；
   - 归置策略：模式（off / inbox / new_files，默认 off）、目录模板（默认 `艺人/专辑/音轨号 - 曲名`，支持自定义模板字符串）；
   - 标签策略：现状全部保留（自动扫描间隔/指纹/单轮上限/时长容差）；
   - AI 策略：现状保留 + 新增「待办预审」开关。
2. **任务**：发起批量任务（歌词抓取/标签扫描/封面抓取/重新入库），范围选择器沿用歌词刮削页的目录树+筛选器（那套做得不错），加任务进度（轮询现状保留）与取消。
3. **历史与规则**：任务历史（现状 tasks 列表升级为 pipeline run 视图，含每文件的处理结果与失败原因）、忽略规则管理（现 `rules` 表 path 级 skip_fetch 的可视化从审查页迁来，扩展为歌词/封面/归置分列的规则表）。

#### 3.3.4 整理页 / 查重页

- **整理页**：只留「一键整理 + 空目录清理」两个 tab（查重独立走）。增加：目标路径**模板预览**（改模板后即时看到示例路径）；「按身份重算」——存量库调整标签后哪些文件的目标路径会变，对比展示。执行条、冲突拦截、lrc 跟随、撤销链路全部保留现状。
- **查重页**：承接现有重复音频全部能力（分类过滤、参照项保护、分类全选、试听、忽略/恢复）。改两点：① 简化默认视图——折叠高级筛选，默认只展示「音频完全一致」和「同一录音」两类（证据最强，适合一键处理），「相似」类收进折叠区逐组人工看；② 每组的「建议保留理由」置顶显示（现状藏在 tag 里）。

#### 3.3.5 概览页

- KPI 卡全部**可点击跳转并带筛选参数**：歌词文件覆盖 → 待办歌词队列；封面完整 → 待办封面队列；标签完整度 → 待办标签队列；其余 KPI → 音乐页对应筛选。
- 「待处理」区升级为待办六类的计数与直达（数据源从 3 行扩为 6 行）。
- 新增「入库流水线」卡：最近 ingest 的文件数、各环节成功率、当前排队量（数据源 §4.9 的 run 视图）。

#### 3.3.6 回收站 / 活动 / 设置

- **回收站**：新增多选批量还原（现状只能逐个）、按批次/文件名搜索、还原目标 root 的选择**移到还原动作内**（现状藏在页头下拉，多 root 用户漏选就报错）；「删除该批次」与「还原」按钮在视觉上拉开（现状红绿混排在一行）。
- **活动页**（原日志）：四 tab 保留（写入/改名/整理/事务），三个批量撤销能力（写入/改名/整理）原样保留；新增全局搜索与「跳转自」——整理页/待办里的撤销承诺全部链到对应 tab。「事务」「恢复冲突」术语加 tooltip 解释（用户文档术语表同步）。
- **设置**：改分组导航（左侧锚点：账户 / 外观 / 自动化策略摘要（链接到自动化页）/ 网络 / AI / API 令牌 / 运行信息）；保存按钮统一为卡片级脏状态提示；代理的按功能开关与 AI 代理开关合并到一张「网络」卡（现状分两卡互相文字指引，易漏）。
- **术语统一表**（全站文案替换，`labels.ts` 集中管理）：「刮削」→「抓取/补全」按语境；「删除」按钮 →「移入回收站」；「采用/写入」→ 队列中叫「采纳」，确认弹窗叫「写入」；「事务」→「写入事务（用于崩溃恢复）」。

### 3.4 统一组件

1. **FieldCompareTable（字段对照）**：合并现状三套（TagEditorPanel 草稿式 / TagLinkDialog 换源式 / TagProposalRow 写入式）。核心形态：字段 | 本地值 | → | 来源值 | 判断 tag | 勾选。三种场景只是数据源与动作不同（编辑器=可编辑本地值；换源=切换候选重渲染；提案=直接写），一套组件加 props 覆盖。
2. **LyricsEditor（歌词编辑器，新增）**：左原文（带时间轴行编辑、行级增删、时间轴微调 ±0.5s 按钮）、右预览（复用 LyricsViewer + 音频对齐播放）。保存时走歌词写入链路（复用 `writer` 字节级写 + sidecar 双写），记录 writes 可撤销。这是「处理能力弱」的直接补强：人工修词不再需要外部编辑器。
3. **CoverPicker（封面选择器）**：来源 tab（同目录缩略图 / 网络搜索结果网格 / URL / 上传）+ 选中预览 + 写入；专辑上下文中提供「应用到整张专辑」。
4. **BatchConfirm（批量确认框架）**：现状 4 处回收站文案、多处 confirm 各写一份，统一为一个组件：列出影响对象（前 N 条 + 计数）、风险说明（按动作类型模板化）、二次输入确认（永久删除才要求）。
5. **TodoItem 外壳**：统一的队列行容器（勾选/主信息/证据/操作插槽），六类队列只填内容。

**窄屏基线**（随 Phase 1 交付，纳入验收）：详情抽屉窄屏全宽（现状 TrackFixDrawer 已是 100%，新抽屉继承）；专辑网格降级为单列卡片列表；待办六队列 tab 横向滚动；批量工具栏吸底。不做「移动端单独设计」，但保证每个新页面有可用的窄屏降级——这也是 §3.1 反对「一页两 tab」论据（tab 套 tab 窄屏不可用）成立的前提。

---

## 4. 自动化能力重构（重点）

### 4.1 统一入库流水线（Ingest Pipeline）

**目标场景**：任何音频文件，扔进 INBOX 根或任意受管根，无需人工干预即完成：身份识别 → 标签补全 → 封面嵌入 → 歌词写入（内嵌 + 同名 .lrc）→ 归位到标准目录。人只在「机器不确定」时被待办叫到。

```
新文件稳定（StabilityGate，现状保留）
        │
        ▼
① 审计与身份解析（audit + identity，新增）
   文件名解析器（新增：拆编号/艺人/曲名/版本标记/feat.）
   → 身份四级：E0 标签 MBID → E1 指纹(可选开启) → E2 目录锚点 → E3 搜索
   → 产出 IdentityDecision{state: resolved|ambiguous|unknown, evidence, recording/album ids}
        │
        ▼
② 并行补全（同一身份，三条支线）
   标签支线：compare_fields → fill/correct/conflict → 按矩阵决定 auto/review（沿用 tag_proposals 状态机）
   歌词支线：TrackQuery 由身份供给（不再裸用脏标签）→ providers 抓取 → match 评分（version 闸门吃身份的版本信息）
            → accept: 写内嵌+写.lrc（沿用 run_apply）；needs_review: 暂存进待办
   封面支线：同目录图 → CAA（release MBID）→ provider 专辑图 → 专辑级一次下载，逐文件嵌入
        │
        ▼
③ 归位（按策略档位）
   mode=off：只生成「归置建议」进待办（diff 视图）
   mode=new_files：对新增文件自动执行（organize 计划落库 → 移动 → 记 organize_ops；冲突转待办）
   INBOX 根：入库完成后移动到 library 根的目标路径（INBOX→LIBRARY 角色流转）
        │
        ▼
④ 质检与报告
   写后复审：歌词时间轴 vs 音频时长、封面尺寸、标签一致性（复用 audit 增量审计）
   产出 ingest 报告：每文件 各环节 结果（done/review/failed/skipped+原因）
   全部完成/失败项汇总 → 待办「失败」队列 + 概览流水线卡
```

工程约束全程遵守：抓取与写入分离（现状原则保留）；写入走写租约 + 备份 + 哈希校验；负缓存与退避重试（`resolutions.next_retry_at`）沿用；`@manual_lease` 用于一切人工触发的写接口；watch 检测到人工等待写租约时让出（`should_yield`）。受管根 `root.read_only=true` 时该根只审计、只产出建议与待办，跳过全部写入支线（补全与归置均不出手）；全局 `policy.write_enabled=false` 等价于全库 L0。

### 4.2 身份解析层（新增模块 `identity.py`）

- **输入**：文件路径、现有标签、目录上下文、（可选）音频指纹。
- **文件名解析器**（新 `filename_parser.py`）：处理真实世界的脏文件名——`01 - 周杰伦 - 晴天.flac`、`1-05 Love Me Do (Remastered 2009).mp3`、`晴天 (Live) - 周杰伦.mp3`、`CD1/02. 歌名.flac`。产出：候选 title/artist/album/track_no/版本标记/feat. 结构。规则覆盖：前导编号（复用 `scan._LEADING_TRACK_NO`）、`艺人 - 歌名` 模式、括号内版本词（复用 `match._VERSION_SYNONYMS` 词表）、多艺人分隔、繁简（复用 `match.normalize_text`）。
- **判定顺序**沿用 `tagscrape` 成熟四级（E0→E1→E2→E3），新增两点：
  1. E3 搜索的查询词在标签缺失/冲突时用文件名解析结果**修正后重试**（这是「不够准确」的最大单点收益：现状标签错→搜索词错→歌词错）；
  2. E4 AI 兜底（可选，默认关）：E3 仍歧义时，把候选列表与文件名/标签/时长交给 AI 出「建议 + 置信度」，**决策权在程序**（复用 `ai.py` 范式：预算上限、置信度阈值、失败当无判断）。
- **产出落库**：`audio_index` 扩展或新表 `identities`（location_id、recording_mbid、release_mbid、state、evidence_json、resolved_at）。供歌词支线、封面支线、归置支线查询。
- **幂等**：身份结果带输入指纹（标签哈希+文件名），输入变了自动重解析（沿用 `resolutions` 的 revision_hash 思路）。

### 4.3 歌词自动化增强

保留现状骨架（`run_fetch` → `stage_lyrics` → `run_apply`，负缓存、重试、租约、熔断全部沿用），增强六点：

1. **TrackQuery 换源**：由身份层供给（E0 直接 MBID 查询；E2/E3 用解析后的干净查询词）。
2. **翻译落盘**（修复现状数据丢弃）：
   - 「双语 LRC」：翻译行紧跟原文行写入 `.lrc` 与内嵌（播放器兼容性最好）；
   - 「独立字段」：FLAC 存 `LYRICS-TRANSLATION` 类 Vorbis 字段（白名单需扩展 `formats.VORBIS_LYRICS_FIELDS`）；
   - 默认值已定案（§7 决策记录 2）：新装 `bilingual_lrc`，升级部署保持 `off` 不改变既有行为；对齐实现见 `lrc.merge_bilingual`（附录 C）。
3. **逐字歌词可保留**（可选）：QRC/KRC 原始文本存 sidecar 旁的 `.qrc`（仅存储不改音频），策略默认关。
4. **needs_review 的 AI 预审**（可选，默认关）：对暂存的 needs_review 候选跑语义比对（候选歌词行 vs 文件名/标题/已有标签），产出「建议采纳/建议驳回 + 置信度」，**在待办行内展示建议**，人不点仍不写入——是降负担不是替人决策。
5. **重试耗尽出口**：`not_found` 三轮用尽的曲目进待办「失败」队列，行内提供「改用文件名线索重查」（换解析器修正的查询词）与「永久跳过（写规则）」两个动作，消灭静默堆积。
6. **质量门增强**：现有时间轴超长/占位词/suspicious 检测保留；新增「对齐率检查」（空行占比、连续重复行、时间轴单调性），不达标的候选自动降级 needs_review。

### 4.4 封面自动化（从 0 到 1）

**前置工程（也是 roadmap 近期项）——封面写入格式扩展**：

| 格式 | 现状 | 目标 | 实现路径 |
| --- | --- | --- | --- |
| FLAC | ✅ 可写 | ✅ | 已有 `flac_rewrite_cover` |
| MP3 | ❌ | ✅ | `id3meta.py` 新增 APIC 帧解析/构造/替换（现有帧读写框架齐全，加一个帧类型） |
| OGG Vorbis / Opus | ❌ | ✅ | `oggmeta.py` 新增 `METADATA_BLOCK_PICTURE`（base64 的 FLAC PICTURE 块，`cover.py` 注释已确认结构等价） |
| 其余 | UNSUPPORTED | 维持 | 明确提示 |

写入仍走 `writer.apply_cover_edit` 扩展后的统一管道（备份/校验/回滚/`manual_lease` 全套继承），移除硬编码的 FLAC 判断，改为按格式的 writer 分派。

**来源链（按优先级）**：

1. **同目录图**（零网络成本，风险最低）：复用 `_find_sibling_cover` 的命名约定（cover/folder/front/album），升级为按目录聚合——检测到即生成专辑级任务，一次嵌入全专辑；默认自动（L1）。
2. **Cover Art Archive**：标签链路已有 `MUSICBRAINZ_ALBUMID`/`RELEASEGROUPID` 字段读写能力（`formats.py` 的 `_MATCH_TAG_KEYS`），直接拼 `coverartarchive.org/release/{mbid}/front-500`，复用 `download_cover`（SSRF 防护、大小流式限制、反 WebP Accept 头全部现成）。
3. **歌词源附带专辑图**：QQ 的 `albummid` 已存入 `Candidate.extra`，网易云专辑接口有 picUrl——作为 CAA 无结果时的补充源（国内可达性更好）。
4. **用户上传/URL**（现状保留，进 CoverPicker）。

**专辑级任务模型**：封面是发行属性不是曲目属性。任务按专辑（目录锚点/发行 MBID）聚合：一次下载 → 校验尺寸 ≥300×300、格式 jpg/png（WebP 下载后不转码，直接拒收换源）→ 逐文件嵌入 → 记录 `writes`。专辑内已有内嵌封面的文件跳过（人工痕迹原则：已有有效封面不动，除非用户在 CoverPicker 明确替换）。

**自动级别**：同目录图嵌入默认 auto（L1）；CAA/网络抓取默认 review（暂存进待办封面队列，人确认后批量嵌入），L2 起自动。缺封面报告按专辑聚合进待办。

### 4.5 标签自动化增强

状态机与人工痕迹保护完全保留（这块设计是好的），增强四点：

1. **搜索来源 verified 率提升**：文件名解析修正查询词 + 身份层共享目录锚点（现状 `_folder_anchor` 只服务标签链路，提升为全系统共享）。
2. **指纹引导开启**：首次设置向导与概览页引导配置 AcoustID key（现状默认关，导致 unknown 身份无兜底）。提供「试扫 50 首看效果」的低门槛入口。key 的写入路径：设置页新增 AcoustID 配置项（存 `settings` 表、掩码显示，沿用「凭据不入日志/页面/导出」规则），`LYRA_ACOUSTID_API_KEY` 环境变量优先级不变。
3. **unmatched 智能出口**：进待办后提供三个动作——「改用文件名重查」「手动链接指定」（现状 TagLinkDialog 保留）+ 新增「按身份重命名」（用解析出的规范名改名，帮助下一轮匹配）。
4. **自动冲突升级**：L3 档位下，`_strong_evidence`（MBID conf≥0.99 / 锚点可信+strength≥0.95 / search verified）的 conflict 字段允许自动纠正（现状这些证据已在 `_auto_resolve_conflicts` 用于提案判定，L3 只是放开写入），其余冲突永远留人。

### 4.6 归置自动化

1. **目录模板**：默认 `艺人/专辑/音轨号 - 曲名`（现状 `organize.build_plan` 规则保留：多艺人 `; ` 连接、ALBUMARTIST 优先、合辑不拆、多碟加碟号、180 字节截断），新增模板配置键支持自定义（字段白名单 + 安全组件化沿用 `safe_component`）。
2. **三种模式**（`policy.organize.mode`）：
   - `off`（默认）：现状行为，纯人工预览执行；但 ingest 的步骤③始终生成「归置建议」进待办（diff：现路径→建议路径），人不执行不动文件；
   - `new_files`：仅对「本进程观察到的新增文件」自动执行归位（有 `added_paths` 事件边界，不会波及存量）；冲突（目标存在/字段不足歧义）一律转待办；
   - `inbox`：INBOX 根专属——扔进去的文件走完整流水线后移入 library 根的标准路径（`RootRole.INBOX` 枚举落地的产品形态，「扔进去就处理好」的字面实现）。
3. **存量库永远人工**：整理页预览+执行流程保留（这就是「先写方案不动存量」的安全边界）。

   作用域规则：`policy.organize.mode` 是全局默认，受管根可用 `root.organize_mode` 覆盖（多根曲库常见「一个库自动、一个库只建议」）；INBOX 根必须显式配置 `root.target_root` 指向某个受管 library 根（多 library 根时归位目标不含糊），未配置的 INBOX 根只产出建议不移动。
4. **联动保留**：`.lrc` 跟随移动、空目录清理（单独预览执行）、`organize_ops` 逐条撤销，全部沿用现状代码。

### 4.7 队列消化与失败处理

| 情形 | 现状 | 重构后 |
| --- | --- | --- |
| 歌词 needs_review 堆积 | 只进不出 | AI 预审建议（可选）+ 待办分组批量 + 建议采纳默认勾选 |
| 标签 unmatched 堆积 | 靠人翻页 | 待办队列 + 三个智能出口（§4.5） |
| 重试耗尽 | 静默停等 | 失败队列 + 换线索重查 + 永久跳过 |
| 写入失败 `failed` | 人工翻 staged 表 | 失败队列 + 原因分类（网络/锁/格式/校验）+ 一键重试 |
| 来源下架 | 单条标记 | 失败队列 + 「重新查询」 |
| 事务恢复冲突 | 日志页术语劝退 | 活动页 + tooltip 解释；能力维持现状（查看/清除记录，不做自动恢复） |

### 4.8 准确性专项（汇总）

1. 文件名解析器进查询链路（§4.2，最高收益）；
2. 身份层统一版本判断：`match.py` 的版本闸门吃身份层的版本标记（文件名+标签+候选三方比对）；
3. AcoustID 引导开启（§4.5）；
4. 歌词质量门：对齐率/单调性检查（§4.3）；
5. 封面尺寸/格式闸门（§4.4）；
6. AI 兜底全程「出建议不出动作」，决策权在程序（E4 永远不直接触发写）；
7. 归置用「同一首」判定标准承接现有硬约束（文件名包含关系不算归属，`AGENTS.md` 红线在归置支线同样生效）。

### 4.9 可观测性

- **任务表升级为 run 视图**：一次批量任务 = 一个 run，含每文件 × 每环节的结果矩阵（`tasks` 表已有，扩展明细表或 JSON 明细），自动化页可下钻到单文件。
- **概览流水线卡**：近 7 天 ingest 数、四环节成功率、当前排队。
- **失败即待办**：任何环节的非瞬时失败都成为待办「失败」队列项（带原因与建议动作），不再有「只有翻日志才知道」的静默失败。
- 预留 webhook 通知（roadmap 中期项，本期只在 run 表留钩子字段）。

---

### 4.10 来源健壮性方案：QQ / 网易云（非官方接口）的反爬与开源选型

**选型定案**：三层防线，默认层维持自研直连，**不引入第三方 API 库作为运行时依赖**；开源项目用作响应结构参考与自建网关部署物。理由：Lyra 只取**歌词文本与专辑封面图**（只读、免登录、无 VIP 资源），是风险最低的使用面；163MusicLyrics 等活跃开源工具多年走同一条公开接口路线，证明该面可持续。

| 层 | 内容 | 现状 |
| --- | --- | --- |
| ① 直连层（默认） | 公开 web/fcgi 接口 + UA/Referer 伪装 + 限流/熔断/预算 + 错误分类 | **已实现**（`providers/qq.py`、`providers/netease.py`），本期加固 |
| ② 网关层（可选） | 每来源可配 `base_url` 指向自建网关/反代 | 网易云已支持；QQ 本期补齐 |
| ③ 契约层（韧性） | 解析版本化 + 契约测试 + 健康面板 + 自动降级 | 本期新增 |

#### 4.10.1 开源生态盘点与取舍

| 项目 | 形态 | 现状（2026-10 检索） | 采用方式 |
| --- | --- | --- | --- |
| Binaryify/NeteaseCloudMusicApi | Node 自建网关 | 原 repo 已被 DMCA 下架、不再维护 | 不直接用。生态已迁移：npm 社区包 `NeteaseCloudMusicApi`（`npx`/Docker 可跑）与 api-enhanced 等 fork——作为**网关部署物**写进部署文档 |
| api-enhanced（neteasecloudmusicapienhanced） | Node 网关，维护 eapi/weapi 逆向 | 社区接棒维护 | 同上，网关候选 |
| pyncm（Python，社区 fork 维护） | 库：weapi/eapi/linuxapi 请求签名 | 维护中 | 仅作**签名算法参考**；若直连被封到必须签名，按其实现补 `providers/netease_sign.py`，仍不自建依赖 |
| qqmusic-api-python（luren-dc；PyPI 包名 `qqmusic-api-python`，repo 迁 GitCode） | 库：搜索/歌词/QRC 解密，async httpx | 维护中 | QRC 解密算法参考，不作依赖 |
| 163MusicLyrics（jitwxs，原 MusicLyricApp，Apache-2.0） | 多来源歌词桌面工具（QQ/网易，QRC/KRC 解密，翻译合并） | 活跃 | **主要工程参照**：来源降级顺序、QRC 处理、双语文体合并的行级对齐做法；Apache-2.0 与本项目 MIT 兼容（引用处附 NOTICE） |
| qaqbuyan/qq-music-api 等 Node/Py 自建 | QQ 自建网关 | 各异 | 网关候选，文档化不承诺 |

**为什么不把 pyncm / qqmusic-api-python 设为依赖**：① 需求面只有「搜索 + 歌词 + 封面」三个只读接口，公开接口已覆盖（现状实测）；② 这些库的能力面（下载、播放、加密流）远超需要，引入等于把它们的 API 变更风险整体搬进核心链路；③ Lyra 的限流/熔断/预算/负缓存/错误分类已自建且经过实测，替换得不偿失；④ 同步 httpx 客户端与 async 库的适配成本。折中做法：解析结构与端点变化时，以这些开源实现为「风向标」修复。

#### 4.10.2 直连层加固（本期落地）

1. **风控特征识别**：响应为 HTML、HTTP 403/412、空 body、`<title>` 含验证/滑块字样 → 新异常类 `ProviderBlockedError`（继承 `ProviderError`）：**不写负缓存**、熔断阈值降为连续 2 次、熔断时长分钟级（区别于普通失败的 5 次/秒级）。
2. `ProviderBlockedError` 与 `SchemaChangedError` 都计入健康表（`provider_health`，附录 A Migration 19），自动化页健康卡可见。
3. UA/请求头集中到 `providers/headers.py`，`providers.user_agent` 可配置（默认现值）；QQ `g_tk=5381`、Referer 策略保持现状（已是实测结论）。
4. 「拒绝 ≠ 无结果」语义保持现状：QQ subcode=-10002、网易云 `nolyric`/`uncollected` 的处理已是代码事实，重构不改。
5. 限流默认值复核：QQ 2s / 网易 1.5s 保持；批量任务 provider 并发 = 1（现状）。

#### 4.10.3 网关层：统一契约（本期落地）

- `[providers.netease] base_url`（**已有**）：路径风格 = NeteaseCloudMusicApi（`/search`、`/lyric`、`/song/detail`），响应结构与直连一致——netease.py 已实现「只换地址不改解析」。
- `[providers.qq] base_url`（**新增**）：契约 = **透明反代**——`base_url` 替换 `https://c.y.qq.com` 根，路径/参数/响应完全一致。nginx / Cloudflare Workers 反代、任何镜像站都能当网关，零适配成本。
- 单一出口原则：配置了网关就不再直连（避免同一账号特征命中两个出口）；网关连续失败 → 自动化页提示，一键清空回直连。
- 文档新增「来源网关」一节：网易云 npm 包 compose 示例、QQ nginx 反代示例；`deploy/` 加示例文件（与 `tests/test_deploy_assets.py` 守卫同步）。

#### 4.10.4 契约层：解析版本化 + 契约测试 + 健康闭环（本期落地）

- 解析契约变更必须 bump `POLICY_VERSION`（`match.py`）；负缓存 key 已含该值 → 结构变更不会让旧负缓存污染新解析（机制现状已有，立为流程规则）。
- `tests/test_provider_contract.py`（新增）：`respx` 录制/回放真实响应样本（脱敏 fixture 入 `tests/fixtures/`），覆盖五类：成功 / 空结果 / 查询拒绝 / 风控页 / 结构变更；网关 `base_url` 拼接单测。流程规则：改解析器先改样本再改代码。
- watch 每日烟雾探测：每 provider 一次最小搜索请求（预算 1，错峰），写 `provider_health`；自动化页健康卡显示 30 天成功率 / 最近错误 / 熔断状态；「立即测试」按钮复用设置页 network test 的交互范式。
- 降级联动：provider 连续 3 次 `SchemaChangedError`/`ProviderBlockedError` → 本轮自动摘除出优先级链（内存态，不改配置），待办「失败」队列给「重新探测」动作；探测成功自动回归。

#### 4.10.5 边界与法律

只取歌词文本与封面图，用于用户自有文件的就地补全展示；不提供任何音频下载、不绕过付费、不上传曲库（与既有红线一致）。docs 增补一节说明非官方接口的属性与自建网关的用户责任。

---

## 5. 技术改造清单

### 5.1 后端

| 模块 | 改动 |
| --- | --- |
| `identity.py`（新） | 身份解析编排：文件名解析 + 四级证据 + AI 兜底；产出 IdentityDecision |
| `filename_parser.py`（新） | 脏文件名 → 结构化字段（纯函数，重点单测对象） |
| `cover.py` | 保持；`download_cover` 复用不变 |
| `writer.py` | `apply_cover_edit` 去掉 FLAC 硬编码，分派 `id3meta`（APIC）/`oggmeta`（METADATA_BLOCK_PICTURE）新写入函数；校验与回滚逻辑按格式扩展 |
| `id3meta.py` | 新增 APIC 解析/构造/替换（帧级，不碰音频帧） |
| `oggmeta.py` | 新增 METADATA_BLOCK_PICTURE 读写（复用 `build_picture_block` base64） |
| `pipeline.py` | `run_fetch` 的 TrackQuery 接身份层；新增 `run_cover_stage`、`run_ingest`（编排①②③④）；翻译落盘；质量门 |
| `watcher.py`/`cli.py` | ingest 编排接入主循环；INBOX 角色处理；`policy.organize`/`policy.cover` 热加载（沿用 30s settings revision 机制） |
| `organize.py` | 模板配置化；「按身份重算」对比接口 |
| `tagscrape.py` | 锚点与搜索结果共享给身份层（提取为可复用函数，行为不变） |
| `api.py` | 新端点：`GET /api/v1/todo`（六类聚合）、`POST /api/v1/tasks/ingest`、`POST /api/v1/tasks/cover-fetch`、`GET /api/v1/cover/search`、`POST /api/v1/tracks/lyrics`（手动编辑写入）、`PUT /api/v1/settings/{cover,organize,lyrics}`、`GET /api/v1/runs/{id}`；全部新写接口套 `@manual_lease()`，路径参数过 `_safe_root_file`/`_resolve_within_root` |
| `models.py` | IssueCode 增加：`COVER_LOW_RES`、`LYRICS_MISALIGNED` 等；`RootRole.INBOX` 启用 |

### 5.2 数据库（迁移版本 +1，迁移前自动备份，同步 `tests/test_db.py` 与 `tests/test_fresh_migration.py`）

- `identities` 表（location_id、recording_mbid、release_mbid、state、evidence_json、input_hash、resolved_at）；
- `cover_albums` 表（album_key、source、image_hash、mime、width、height、applied_count、fetched_at）——专辑级封面缓存，避免同专辑重复下载；
- `rules.action` 扩展：`skip_cover`、`skip_organize`（沿用现有 scope 机制）；
- `tasks` 明细扩展（run/step/result JSON）；
- `staged_lyrics` 增加 `translation_text`（可选档启用时用）。

### 5.3 前端

- 新视图：`MusicView`（专辑网格+曲目+文件三模式）、`TodoView`、`AutomationView`；改视图：`OrganizeView`（瘦身）、`OverviewView`、`TrashView`、`LogsView→ActivityView`、`SettingsView`（分组导航）；
- 路由重定向表（§3.2）；侧栏分组与徽标调整；
- 新组件：`FieldCompareTable`、`LyricsEditor`、`CoverPicker`、`BatchConfirm`、`TodoItem` 外壳、`AlbumCard`；
- `labels.ts`：新枚举（IssueCode 新增、IdentityState、CoverSource、OrganizeMode、TodoKind）全部配中文标签（守卫 `tests/test_web_app.py` 强制）；模板禁词检查照常遵守；
- 三种文案统一按 §3.3 术语表全站替换。

### 5.4 配置键（同步 `config.py` 白名单 + `src/lyra/templates/lyra.example.toml` + `deploy/lyra.container.toml.example` + docs；`tests/test_templates.py` 会拒绝未知键）

```toml
[policy.lyrics]
write_translation = "bilingual_lrc"  # 新装默认；升级部署 off（§7 决策记录）| off | bilingual_lrc | separate_field
keep_word_level = false              # QRC 原文存 .qrc
ai_pre_review = false

[policy.cover]
auto_embed_sibling = true
fetch_sources = ["caa"]              # caa | provider
min_min_pixels = 90000               # 300×300
auto_write = false                   # L2 起为 true

[policy.organize]
mode = "off"                         # off | new_files | inbox（全局默认，可被 root.organize_mode 覆盖）
template = "{artist}/{album}/{track_no} - {title}"

[providers.qq]
base_url = ""                        # 透明反代网关根（§4.10.3），空 = 直连
```

根级新键 `root.organize_mode`、`root.target_root`（INBOX→library 映射）加入 `[[roots]]` 模板示例，与上表同步进两份模板与 docs。

### 5.5 测试与文档（按仓库守卫）

- 新增：`test_filename_parser.py`、`test_identity.py`、`test_cover_write.py`（MP3 APIC / OGG picture）、`test_ingest.py`（tmp_path 副本曲库）、`test_todo_api.py`；
- 前端：`FieldCompareTable`/`LyricsEditor` 的 vitest（`.spec.ts`，node 环境）；
- docs：`usage.md`（新页面结构）、`configuration.md`（新键）、新增 `automation.md`（流水线与档位说明）；`mkdocs build --strict` 通过；
- CI 顺序保持：web build → ruff → pytest；前端 typecheck → test → build。

---

## 6. 分阶段实施计划

每阶段独立可发布、独立回滚，顺序有依赖：

**Phase 1：信息架构与体验（无新自动化）**
- 页面重组与路由重定向（§3.2）；待办页**六队列骨架一次成型**（歌词/标签/孤儿三类先接内容，封面/归置/失败先占位，Phase 2–4 填充）、自动化页骨架；统一字段对照组件；歌词编辑器；Overview 跳转带参；回收站批量还原；文案术语统一；窄屏基线（§3.4）。
- 验收：旧地址全部可达新位置；歌词审查（含孤儿 tab 与 AI 判重）/标签审查的全部现有能力在待办页可用；`pytest` + vitest 全绿。

**Phase 2：封面从 0 到 1 + 来源健壮性**
- MP3/OGG/Opus 封面写入；同目录图专辑级自动嵌入；CAA 抓取（review 档）；CoverPicker；封面队列进待办；缺封面 KPI 可跳转。
- 来源健壮性（§4.10 全部四小节）：`ProviderBlockedError`、QQ 网关 `base_url`、`provider_health` 表与每日烟雾探测、契约测试、自动化页健康卡。
- 验收：FLAC/MP3/OGG 样本写入封面后哈希校验通过、可撤销；一张 12 曲专辑一次操作全部嵌入；模拟风控响应（fixture）时不写负缓存且熔断可见。

**Phase 3：身份层与入库流水线**
- `identity.py` + 文件名解析器；歌词 TrackQuery 接身份；翻译落盘；质量门；失败队列与重试出口；AI 预审（可选）；run 视图。
- 验收：脏标签样本文件的歌词匹配正确率提升（用测试曲库回归集量化）；ingest 报告可下钻。

**Phase 4：自动归置（红线变更，最后做）**
- 模板配置化；`policy.organize.mode`；INBOX 角色流转；归置建议进待办；冲突拦截。
- 验收：INBOX 扔入 → 全流程 → 落位标准目录 → `organize_ops` 可整批撤销；`mode=off` 时行为与现状完全一致。

**全自动启用路径（目标场景「扔进去就处理好」的推荐配置）**：默认档位偏保守（L1 + `organize.mode=off` + 指纹关），此时流水线只产出报告与待办建议、不会误写文件。要达到全自动，在首次设置向导或设置页完成三步：

1. 开启指纹：填入 AcoustID key（设置页新增项，或 `LYRA_ACOUSTID_API_KEY`）；
2. 档位调至 L2（或按模块覆盖：`policy.cover.auto_write=true`；归置 `mode=new_files`，或建一个 `role=inbox` + `target_root` 的受管根，把新歌扔进 INBOX）；
3. 可选：歌词 `write_translation="bilingual_lrc"`、`ai_pre_review=true`。

---

## 7. 风险与决策记录

**风险**：

| 风险 | 缓解 |
| --- | --- |
| QQ/网易云接口变更、反爬（非官方 API） | 三层防线（§4.10）：直连加固 + 可选自建网关 + 契约测试与健康面板；单来源失败自动降级下一来源，不阻塞流水线、不污染负缓存 |
| CAA/MB 国内可达性 | 直接复用现有按功能代理开关（`PROXY_FEATURES` 已含 `cover` 粒度，无需新增） |
| 自动归位误判移动文件 | 三重保险：默认 off、仅新文件、全程 organize_ops 可撤销；E3 以下证据只出建议 |
| OGG 页级重排写封面的损坏风险 | 沿用 `oggmeta` 的「comment 与音频同页则拒绝」语义；写入后音频哈希校验失败即回滚（writer 现有机制） |
| 大库首次 ingest 速率 | 复用单轮上限模式（`max_auto_write_per_run` 同款）；负缓存防重复抓取；provider 限流器沿用 |
| 收件箱聚合端点性能 | 分页 + 惰性计数（tab 计数异步加载），不做全库 UNION 大查询 |

**决策记录**（原开放问题已全部定案，实施按此执行）：

1. **归置红线**：采纳「默认不动 + 显式开启 + 仅新文件 + 全程可撤销」（§2.3 / §4.6）。理由：自动归位是本重构的明确目标；护栏三重——模式默认 `off`、只碰新入库文件（存量永远人工预览）、`organize_ops` 逐条撤销 + 回收站兜底。INBOX 流转与 `new_files` 均为显式开启项。
2. **翻译歌词**：新装默认 `bilingual_lrc`（双语文体，本地播放器生态的事实标准，163MusicLyrics 等工具同样采用合并翻译）；升级部署保持 `off` 不改变既有行为，设置页引导开启。落地见 `lrc.merge_bilingual`（附录 C）。
3. **逐字歌词**：默认 `keep_word_level=false`；实现 QRC 原文落 `<同名>.qrc` sidecar 的能力（开关可开）。理由：逐字歌词的播放器支持少，但上游数据丢弃不可逆，保留成本极低（一个 TEXT 列 + 一个可选文件）。
4. **AI 预审**：默认关；开启时预算沿用 `ai.max_requests_per_run`（12），新增 `ai.pre_review_confidence=0.8`；仅当 needs_review 队列 > 50 条时启用（量小不值得花预算）。AI 永远只出建议不出动作（§2.2 矩阵 E4 列）。
5. **移动端**：Phase 1 交付窄屏基线（§3.4）；深度的移动端审查体验（逐行对比、批量手势）排在四阶段之后。
6. **来源反爬**：三层防线（§4.10），默认直连、不自建依赖、开源项目只做参考与网关部署物。
7. **近似匹配分级**（2026-10-29 增补）：版本不一致（如现场版拿到录音室词）但标题核心与艺人一致、时长在硬闸门内的候选定为「近似匹配」——AI 判定同曲异版且高置信时自动采用；无 AI 时归入待办的「近似可接受」组供批量确认。**「毫不相关」的候选绝不以近似名义放行**：标题核心不匹配一票否决。`policy.matching.allow_approximate` 控制。
8. **低风险标签纠正自动化**（2026-10-29 增补）：强证据来源（E0/E1/核验搜索）把英文/罗马字标题纠正为中文（或反向）这类「文字体系切换」纠正视为低风险，自动写入不进人工队列。`policy.tags.low_risk_auto_correct` 控制。
9. **AI 判定升级**（2026-10-29 增补）：E4 从「只出建议」升级为「高置信可自动采纳」——`ai.pre_review` + `ai.pre_review_confidence` + `ai.auto_accept`；判定全文留档 `staged_lyrics.ai_json`，可回溯、可撤销；模型永远不绕过程序闸门（时长/版本/标题核心）。

---

---

## 附录 A：数据模型与迁移 DDL

现状 `SCHEMA_VERSION = 15`。以下迁移从 16 起逐版递增，跨进程文件锁由 `db.migrate()` 处理，迁移前自动备份；每个版本同步 `tests/test_db.py` 与 `tests/test_fresh_migration.py`。

**Migration 16 —— 身份层**

```sql
CREATE TABLE identities (
    location_id      INTEGER PRIMARY KEY REFERENCES locations(id) ON DELETE CASCADE,
    state            TEXT NOT NULL,               -- resolved | ambiguous | unknown
    recording_id     TEXT NOT NULL DEFAULT '',
    release_id       TEXT NOT NULL DEFAULT '',
    release_group_id TEXT NOT NULL DEFAULT '',
    evidence         TEXT NOT NULL DEFAULT '',    -- E0..E3（AI 兜底时 E4）
    confidence       REAL,
    source           TEXT NOT NULL DEFAULT '',    -- tags_mbid | fingerprint | folder_anchor | search | ai
    query_used       TEXT NOT NULL DEFAULT '',    -- 实际命中的查询词（失败重查直接复用）
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    input_hash       TEXT NOT NULL,               -- sha256(标题+艺人+专辑+文件名+目录名+标签MBID)
    resolved_at      REAL NOT NULL
);
CREATE INDEX idx_identities_state ON identities(state);
```

语义：`input_hash` 不匹配即重算（幂等）；`identities` 只存「解析结论」，网络失败的退避重试仍由 `resolutions` 负缓存管理，两者职责不重叠。

**Migration 17 —— 专辑封面缓存**

```sql
CREATE TABLE cover_albums (
    id            INTEGER PRIMARY KEY,
    album_key     TEXT NOT NULL UNIQUE,  -- release_id；无身份时 root_id + ':' + 规范化目录
    source        TEXT NOT NULL,         -- sibling | caa | provider | manual
    image_hash    TEXT NOT NULL,         -- sha256
    file_path     TEXT NOT NULL,         -- data/cover-cache/<hash>.<ext>（字节落盘，不进库）
    mime          TEXT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    fetched_at    REAL NOT NULL,
    applied_count INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT 'ready'  -- ready | rejected（质量门不过）
);
```

**Migration 18 —— 歌词翻译与逐字**

```sql
ALTER TABLE staged_lyrics ADD COLUMN translation TEXT;  -- 上游翻译原文（现状抓了即弃）
ALTER TABLE staged_lyrics ADD COLUMN raw_qrc TEXT;      -- keep_word_level=true 时存 QRC 原文
```

`rules.action` 扩展枚举值 `skip_cover` / `skip_organize`（TEXT 列，无 DDL；UNIQUE (scope, target, action) 不变）。

**Migration 19 —— 任务明细与来源健康**

```sql
CREATE TABLE task_steps (
    id          INTEGER PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,       -- identity | tags | lyrics | cover | organize | verify
    location_id INTEGER,
    rel_path    TEXT,
    state       TEXT NOT NULL,       -- done | review | failed | skipped
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_task_steps_task ON task_steps(task_id, seq);

CREATE TABLE provider_health (
    provider     TEXT NOT NULL,
    day          TEXT NOT NULL,      -- 'YYYY-MM-DD'（按 timezone 配置的本地日期）
    ok           INTEGER NOT NULL DEFAULT 0,
    not_found    INTEGER NOT NULL DEFAULT 0,
    blocked      INTEGER NOT NULL DEFAULT 0,
    schema_error INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    PRIMARY KEY (provider, day)
);
```

---

## 附录 B：API 契约（新增端点）

统一约束：全部挂 `require_auth`；写操作另套 `@manual_lease()`；路径参数一律过 `_safe_root_file` / `_resolve_within_root`；`policy.write_enabled=false` 或根 `read_only` 时写端点 409/403（沿用现有语义）；错误走 FastAPI `detail`。

| 端点 | 方法 | 请求 | 响应 / 语义 |
| --- | --- | --- | --- |
| `/api/v1/todo` | GET | `kind`(六类,可空) `state` `page` `size` | `{counts:{lyrics,tags,covers,organize,orphans,failed,total}, items:[TodoItem]}`；counts 全量、items 分页（惰性聚合） |
| `/api/v1/tracks/apply-lyrics` | POST（**扩展**） | 现有参数外新增：`text`、`translation`（与 `provider`/`provider_id` 二选一） | 手动编辑直写模式；走既有歌词写入链（备份/校验/事务）；目标已有 synced 且 `force=false` 时 409 |
| `/api/v1/cover/candidates` | GET | `root` `path` | `{album_key, candidates:[{source, ref, width, height, mime}]}`；只列不落字节，应用时再经 `download_cover` |
| `/api/v1/tasks/cover-fetch` | POST | `{root, dirs:[] \| album_key, apply=false}` | 按专辑抓取；`apply=false` 只进待办封面队列 |
| `/api/v1/tasks/ingest` | POST | `{roots?:[], paths?:[], scope:"new"\|"all"}` | 手动重跑入库流水线；返回 task id |
| `/api/v1/runs/{task_id}` | GET | `step_page` | `{task, steps:[{seq,kind,rel_path,state,detail}]}` |
| `/api/v1/organize/suggest` | GET | `root` | `[{rel_path, current, suggested, conflict}]`（ingest 步骤③产物） |
| `/api/v1/inbox/process` | POST | `{root, dry_run=true}` | INBOX→library 流转预览/执行 |
| `/api/v1/settings/lyrics` `/cover` `/organize` | GET/PUT | 对应附录 E 键 | 沿用 settings 表 + `settings.revision` 热加载 |
| `/api/v1/settings/acoustid` | GET/PUT | `{api_key}` | 写 settings、掩码返回；`LYRA_ACOUSTID_API_KEY` 优先级不变 |
| `/api/v1/providers/health` | GET | — | `[{provider, days:[…], breaker, last_error}]` |

`TodoItem`：`{kind, ref, root, rel_path, title, artist, album, summary, evidence:{score, source, gates[]}, default_checked, actions:[{action, label, danger}]}`。六类的数据源：歌词=`staged_lyrics`、标签=`tag_proposals`、封面=audit 问题码 + `cover_albums`、归置=`organize` 建议产物、孤儿=orphan sidecars、失败=failed staged + 重试耗尽 `resolutions` + `task_steps` failed。封面类可复用现有 `/api/v1/issues` 的查询口径。

---

## 附录 C：后端模块规格（函数级）

**`filename_parser.py`（新，纯函数零 IO）**

```python
@dataclass(frozen=True, slots=True)
class FilenameHints:
    title: str; title_conf: float
    artists: tuple[str, ...]; artists_conf: float
    album: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    version_tokens: tuple[str, ...] = ()   # live/remix/…（复用 match 词表）
    feat: tuple[str, ...] = ()

def parse_filename(stem: str, *, parent_dir: str = "") -> FilenameHints: ...
```

规则序（短路）：前导编号剥离（复用 `scan._LEADING_TRACK_NO`）→ `艺人 - 歌名`（`parent_dir` 规范化后等于艺人名时升置信）→ `艺人 - 专辑 - 歌名` → `序号. 歌名` → 纯歌名。括号段折叠：版本词入 tokens、`feat./ft.` 入 feat。繁简/全角归一复用 `match.normalize_text`。

**`identity.py`（新）**

```python
@dataclass(frozen=True, slots=True)
class IdentityDecision:
    state: str                  # resolved | ambiguous | unknown
    recording_id: str = ""
    release_id: str = ""
    evidence: str = ""          # E0..E4
    confidence: float | None = None
    source: str = ""            # tags_mbid | fingerprint | folder_anchor | search | ai
    query_used: str = ""
    candidates: int = 0

def resolve_track(root_cfg, rel_path, *, tags: dict, audio: AudioFacts,
                  fingerprint: str | None = None) -> IdentityDecision: ...
def resolve_album(root_cfg, dir_path, *, members: list[dict]) -> IdentityDecision: ...
def sync_root_identities(root) -> SyncReport   # input_hash 变化的行重算（幂等）
```

判定顺序 = §2.2 矩阵来源序：`tags_mbid`(E0) → `fingerprint`(E1，可选) → `folder_anchor`(E2，把 `tagscrape._folder_anchor` 提取为可 import 的纯函数) → `search`(E3，查询词 = 标签字段，缺失字段由 FilenameHints 补) → `ai`(E4，可选)。E0 时歌词支线直接走 MBID 查询（`tagsources` 补 recording 级查询函数）。

**`pipeline.py`（扩展）**

```python
def run_ingest(root, rel_paths: list[str], *, cfg) -> str:   # 返回 task_id
    # 每文件：audit 增量 → identity.resolve_track
    #   → 支线并行：tags（单文件口径 scan_tags）/ lyrics（TrackQuery 由 IdentityDecision 构造）
    #              / cover（run_cover_stage）
    #   → organize 建议或执行（policy.organize.mode + root.read_only 把关）
    #   → verify（audit 复审）→ task_steps 明细

def run_cover_stage(root, dirs: list[str], *, apply: bool) -> str:
    # 专辑聚合（cover_albums.album_key）→ 来源链 sibling→caa→provider
    #   → 质量门（jpg/png + min_min_pixels）→ apply 时逐文件 apply_cover_edit
```

翻译落盘：`RawLyrics.translation` 存 `staged_lyrics.translation`；写入时按 `policy.lyrics.write_translation`——`bilingual_lrc` → `lrc.merge_bilingual(text, translation)`；`separate_field` → 仅 FLAC 追加 `LYRICS-TRANSLATION`（`formats.VORBIS_LYRICS_FIELDS` 同步扩展）；`off` → 忽略。逐字保留：`raw_qrc` 列存 QRC 原文；`keep_word_level=true` 时写 `<同名>.qrc`。

**`lrc.py`（扩展）**

```python
def merge_bilingual(text: str, translation: str, *, tolerance_ms: int = 800) -> str:
    # 翻译行按最近时间轴对齐（netease tlyric / qq trans 自带时间轴）；
    # 容差外丢弃；输出「原文行 + 同时间轴翻译行」文体
```

**`writer.py` / `id3meta.py` / `oggmeta.py`（封面写入分派）**

```python
# writer.py
def apply_cover_edit(plan: CoverEditPlan) -> CoverEditOutcome:
    # FLAC → flac_rewrite_cover（现状）；MP3 → id3_rewrite_cover（新）；
    # OGG/Opus → ogg_rewrite_picture（新）；其余格式维持明确报错

# id3meta.py（新增）
def id3_rewrite_cover(source, target, *, picture: bytes, mime: str,
                      replace_type: int = 3) -> None: ...
# 只替换 picture type=3 的 APIC 帧（与 FLAC「同类型替换」语义一致）；
# v2.3 latin-1 mime / v2.4 UTF-8（沿用 _frame_encoding_for_version）；
# description 置空；其它 APIC 类型与未知帧逐字节保留；padding 优先，不足按 NEW_TAG_PADDING 扩

# oggmeta.py（新增）
def ogg_rewrite_picture(source, target, *, picture_block: bytes | None) -> None: ...
# picture_block=None 移除；写 METADATA_BLOCK_PICTURE（build_picture_block 的 base64）；
# 清除遗留 COVERART / COVERARTMIME / COVERARTTYPE；comment 与音频同页不可分离时维持「明确拒绝」
```

校验链不变：音频哈希 + 非目标块逐字节比对 + 原子替换 + 失败回滚。

**`cover.py`（扩展）**

```python
def caa_front_url(release_id: str, size: int = 500) -> str
    # https://coverartarchive.org/release/{mbid}/front-500；download_cover 已 follow_redirects
def cover_quality_ok(info: CoverInfo, *, min_pixels: int) -> bool
    # 自动链只收 jpg/png（拒 webp/gif，让来源链换下一来源）；手动上传沿用现状
```

**`organize.py`（扩展）**

```python
def template_rel_path(tags: dict, template: str) -> str
    # 字段白名单 {artist, albumartist, album, title, track_no, disc_no, year, ext}；
    # 组件安全化沿用 safe_component（180 字节截断）；默认模板语义与现状 build_plan 一致

def suggest_for_root(root) -> list[OrganizeSuggestion]   # ingest 步骤③数据源

def move_cross_root(src_root, src_rel, dst_root, dst_rel) -> OrganizeOp
    # INBOX→library 跨根流转：copy 到目标 .lyra-ingest-*.tmp → 校验音频哈希 →
    # 源文件移入回收站（记录 trash 路径）→ 原子 rename → 记 organize_ops{src,dst,trash}
    # undo：目标移回收站 → 源从回收站还原 → 清 tmp
```

**`watcher.py` / `cli.py`**：`_process_new_files` 尾部接 `run_ingest`；新增 `_provider_probe_worker`（每日烟雾探测，错峰）；`run_fetch` 的 TrackQuery 构造改为 IdentityDecision 供给。

---

## 附录 D：前端规格

路由（`web/src/router/index.ts`）：

```ts
{ path: '/music', name: 'music', component: MusicView,
  meta: { title: '音乐', group: '曲库', icon: 'Headset' } },
{ path: '/todo', name: 'todo', component: TodoView,
  meta: { title: '待办', group: '维护', icon: 'Bell', badge: 'todoTotal' } },
{ path: '/automation', name: 'automation', component: AutomationView,
  meta: { title: '自动化', group: '维护', icon: 'MagicStick' } },
// 重定向：/library→/music，/files→/music?view=files，/review→/todo?kind=lyrics，
//         /scrape→/automation?tab=lyrics，/tags→/todo?kind=tags，/logs→/activity
```

新组件（props / emits）：

| 组件 | props | emits | 说明 |
| --- | --- | --- | --- |
| `FieldCompareTable.vue` | `fields: FieldRow[]`（name/local/incoming/verdict/locked）、`editable?`、`v-model:checked` | `change` | 合并三套对照 UI；verdict 走 `labels.ts` |
| `LyricsEditor.vue` | `root, path, initialText, translation?` | `save(payload)` | 左编辑右预览；时间轴行微调 ±0.5s；保存走 `apply-lyrics` 的 text 模式 |
| `CoverPicker.vue` | `root, path, albumKey?` | `changed` | 四来源 tab + 缩略图 + 「应用到整张专辑」（专辑上下文时） |
| `TodoItem.vue` | `item: TodoItem`（插槽 `evidence`/`actions`） | `resolve(action)` | 六类队列共用外壳 |
| `AlbumCard.vue` | `album: AlbumSummary` | `open` | 角标：缺封面/缺歌词/待审查计数 |
| `BatchConfirm.vue` | `title, items: string[], risk: 'reversible'\|'permanent', open` | `confirm, cancel` | 统一确认文案与风险分级 |

stores：`meta` 增加 `todoCounts`（`/todo` counts，随 45s 可见性轮询刷新），侧栏徽标与概览卡消费。`labels.ts` 新枚举中文映射：`TODO_KIND`、`IDENTITY_STATE`、`COVER_SOURCE`、`ORGANIZE_MODE`、`STEP_STATE`、`PROVIDER_HEALTH`（`tests/test_web_app.py` 守卫）。api client 新增：`todo()`、`saveLyrics()`、`coverCandidates()`、`coverFetchTask()`、`ingestTask()`、`run()`、`organizeSuggest()`、`inboxProcess()`、`settingsLyrics/Cover/Organize/Acoustid()`、`providerHealth()`。文案红线延续：Vue 模板不出现 sidecar/provider/pipeline 等内部词，枚举一律走 labels。

---

## 附录 E：配置键全表

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `policy.lyrics.write_translation` | str | 新装 `bilingual_lrc`；升级 `off` | off / bilingual_lrc / separate_field |
| `policy.lyrics.keep_word_level` | bool | false | QRC 原文落 `.qrc` |
| `policy.lyrics.ai_pre_review` | bool | false | 待办预审 |
| `policy.lyrics.write_embedded` / `write_sidecar_lrc` | bool | true / true | 现状键沿用 |
| `policy.cover.auto_embed_sibling` | bool | true | 同目录图自动嵌入 |
| `policy.cover.fetch_sources` | list | `["caa"]` | caa / provider |
| `policy.cover.min_min_pixels` | int | 90000 | 300×300 |
| `policy.cover.auto_write` | bool | false | L2 起 true |
| `policy.organize.mode` | str | off | off / new_files / inbox |
| `policy.organize.template` | str | `{artist}/{album}/{track_no} - {title}` | 字段白名单见附录 C |
| `root.organize_mode` | str | 继承全局 | 根级覆盖 |
| `root.target_root` | str | `""` | INBOX→library 映射 |
| `providers.qq.base_url` | str | `""` | 透明反代网关根（新） |
| `providers.netease.base_url` | str | `""` | NeteaseCloudMusicApi 风格（已有） |
| `providers.user_agent` | str | 现 Chrome UA | 直连伪装头 |
| `ai.pre_review_confidence` | float | 0.8 | 预审建议阈值 |
| `retry.*` | — | 不变 | 现状沿用 |

同步位：`config.py` 键白名单 + `src/lyra/templates/lyra.example.toml` + `deploy/lyra.container.toml.example` + `docs/configuration.md`；`tests/test_templates.py` 拒绝未知键。

---

## 附录 F：测试矩阵（按仓库守卫）

| 测试 | 覆盖 |
| --- | --- |
| `tests/test_filename_parser.py`（新） | 解析规则全分支：编号 / 艺人-歌名 / 三段式 / 括号版本 / feat / 繁简 |
| `tests/test_identity.py`（新） | E0→E3 判定序、`input_hash` 失效重算、AI 兜底关时不调用、unknown 不污染负缓存 |
| `tests/test_provider_contract.py`（新） | respx 回放五类响应（成功/空/拒绝/风控/结构变更）；网关 `base_url` 拼接；降级摘除与回归 |
| `tests/test_cover_write.py`（扩展） | MP3 APIC 替换（v2.3/v2.4）、OGG `METADATA_BLOCK_PICTURE`、非目标帧/页逐字节保留、音频哈希、回滚 |
| `tests/test_ingest.py`（新） | tmp_path 副本曲库：新文件→身份→三支线→归位→报告；read_only 根零写入；INBOX 跨根流转与撤销 |
| `tests/test_todo_api.py`（新） | 六类聚合、分页、counts 一致性、写接口租约/只读语义 |
| `tests/test_web_app.py` | 新页面/分包存在性、labels 全覆盖、模板禁词 |
| `tests/test_templates.py` | 新配置键进两份模板 |
| `tests/test_ui_copy.py` | 新文案纯文本 |
| web vitest（`*.spec.ts`） | `FieldCompareTable`、`LyricsEditor` 纯逻辑、双语合并前端预览 |

---

## 附录 G：分阶段文件改动清单

**Phase 1（信息架构）**：改 `web/src/router/index.ts`；新增 `MusicView/TodoView/AutomationView/ActivityView` 与六组件（附录 D）；`api/client.ts` + `api/types.ts` + `utils/labels.ts`；后端 `api.py`（`/todo`、`apply-lyrics` text 模式、路由兼容）；歌词手动编辑复用现有写入链。无 DB 迁移（todo 聚合查现有表）。

**Phase 2（封面 + 来源健壮性）**：`writer.py`、`id3meta.py`、`oggmeta.py`、`cover.py`、`pipeline.py`（`run_cover_stage`）、`api.py`（cover/candidates、tasks/cover-fetch、settings/cover）；Migration 17；providers：`headers.py`（新）、`ProviderBlockedError`、`providers.qq.base_url`、`provider_health` 表 + 烟雾探测（Migration 19）、`tests/test_provider_contract.py`；docs「来源网关」一节 + `deploy/` 示例。

**Phase 3（身份 + 流水线）**：`filename_parser.py`、`identity.py`、`pipeline.py`（`run_ingest`、TrackQuery 换源、翻译/逐字落盘）、`lrc.py`（`merge_bilingual`）、`formats.py`（翻译字段白名单）、`tagscrape.py`（锚点提取复用）；Migration 16 / 18；`api.py`（tasks/ingest、runs、providers/health、settings/lyrics、settings/acoustid）；自动化页 run 视图与 AI 预审开关。

**Phase 4（归置）**：`organize.py`（template / suggest / move_cross_root）、`models.py`（`RootRole.INBOX` 启用）、`api.py`（organize/suggest、inbox/process、settings/organize）；`[[roots]]` 模板新键（`root.organize_mode`、`root.target_root`）；待办归置/失败队列填充；docs 红线变更说明（§7 决策记录 1）。

---

## 8. 自检记录

按用户要求，方案完成后进行多轮独立自查（三轮自检 + 一轮换眼的独立评审），每轮记录维度、发现与修正：

**第一轮：需求覆盖检查（对照用户原话逐条映射）**
- 「深挖所有功能」→ §1.1 盘点 11 页 + §3.2 路由迁移映射表逐页承接；检查发现：初稿遗漏了「文件」页的非音频文件管理（预览/批量删除）——已并入音乐页「文件」视图（§3.3.1）与待办承接说明；遗漏了孤儿 LRC 的 AI 判重去向——已写入待办「孤儿」队列（§3.3.2）。
- 「音乐 = 歌词/封面/标签三大模块」→ §3.3.1 详情抽屉三模块逐一设计，另确认「文件」模块保留（改名/替换/删除是真实需求，砍掉会丢功能）。
- 「操作与审查 tab vs 多页」→ §3.1 三方案对比 + 明确推荐 C + 论证。
- 「任何歌曲扔进去自动处理好」→ §4.1 流水线覆盖 归位+标签+封面+歌词四环节；歌词「含内嵌和 lrc」双写在 §4.1 步骤②与 §4.3 明确（现状 `run_apply` 已双写，方案保留并写明）。
- 「不做开发」→ 本文档仅为方案，无代码改动。
- 「自主检查 3 次以上」→ 本节即记录。

**第二轮：技术可行性对照代码事实检查**
- 逐条核对关键断言：封面写入现状仅 FLAC（`writer.py` `apply_cover_edit` 硬编码）✅；翻译字段 `RawLyrics.translation` 在 `pipeline.py`/`writer.py` 零引用 ✅（grep 验证）；`RootRole.INBOX` 仅预留枚举 ✅（`models.py:31`）；`run_apply` 双写内嵌+lrc 受 `write_sidecar_lrc` 控制 ✅（`pipeline.py:1160`）。
- 发现并修正：初稿把「lrc→内嵌」说成缺失方向，实际上 `run_apply` 写入时就同时写内嵌和 `.lrc`——修正为「现状已双写，保留」；CAA 部分补充「`formats.py` 的 `_MATCH_TAG_KEYS` 已含 MBID 字段读写，CAA 无需新查询链路」；OGG 封面写入补充 `oggmeta` 拒绝语义的风险行。
- 检查配置键与现有键冲突：`policy.lyrics.*` 与现有 `policy.write_sidecar_lrc` 等并存关系在 §5.4 标注为新增分组，不迁移旧键（向后兼容）。

**第三轮：一致性 / 边界 / 遗漏检查**
- 人工痕迹保护在所有自动化支线的适用性逐条过：歌词忽略规则（`rules` skip_fetch）✅ 保留；标签 ignored/done/link ✅ 保留；封面新增同类机制——专辑已有有效内嵌封面默认不覆盖、`skip_cover` 规则 ✅（§4.4、§5.2 补充）；归置对人工整理过的目录不重排——现状 `organize_plans` 只在人工执行，`new_files` 模式天然只碰新文件 ✅。
- 边界用例检查：多受管根、只读根、`write_enabled=false`（L0 等价）、监听降级轮询（rescan 兜底 ingest，现状 30s 对账机制承接）、跨进程租约（ingest 写入全走租约+让出）列入检查维度；本轮仅完成清单核对，其中只读根与多根 ingest 语义**未真正落进正文**——该缺口由第四轮评审抓出，补全见 §4.1 约束段与 §4.6 作用域规则。
- 发现并修正：初稿未提「审查确认前的歌词 diff」——补充复用 AlternativesDrawer 双栏对比（§3.3.1）；未提回收站「还原目标 root」交互缺陷——补充进 §3.3.6；`organize` 模板配置补充 180 字节截断与 `safe_component` 沿用（§4.6）。
- 遗漏扫描（对照 §1.1 功能表逐行核对 §3 新页面）：全部 11 页的每项操作都能在新 IA 中找到归宿；无「计划删除但未安排承接」的功能。

**第四轮：独立评审（换一双眼睛，对照代码全文复核）**
由未参与撰写的评审代理对成稿做四维审查（需求覆盖 / 功能遗漏 / 事实抽查 / 内部矛盾），给出约 20 处发现（其中 5 处判定为必须修），全部处理完毕：

- 事实修正：`_MATCH_TAG_KEYS` 模块指认（tagsources → 实为 `formats.py`）；代理「封面」粒度已存在（`PROXY_FEATURES` 已含 `cover`，风险表改为直接复用）；概览图表 6 组 → 7 组；标签提案 `done` 是数据层状态而非页面 tab，承接表述已改。
- 矛盾修正：翻译歌词默认值三处打架 → 统一为「默认 `off` 兼容现状，设置页引导开启双语」；封面网络抓取自动级别：矩阵与 §4.4/§5.4 统一为「L1 review、L2 起 auto」；L0/L1/L2 与现状映射重写（`write_enabled=false → L0`，`write_mode=auto → L1` 行为不变，避免升级部署行为漂移）。
- 设计补全：受管根 `root.read_only` 的 ingest 行为落正文（§4.1）；归置模式作用域与 INBOX→library 目标映射 `root.target_root`（§4.6）；AcoustID key 写入路径与 env 优先级（§4.5）；活动页三个批量撤销点名承接；事务恢复冲突如实写「维持现状，不做自动恢复」；孤儿队列点名「设为这首的歌词」；Phase 1 改为六队列骨架一次成型（避免孤儿队列在过渡期悬空）；`/tags` 重定向改为单一去向；移动端窄屏基线成文（§3.4）；新增档位×模块行为对照表与全自动启用路径（§2.2、§6），回答「开哪些开关才算扔进去就处理好」。

**第五轮：增量修订检查（按用户反馈细化到开发级）**

本轮修订内容：① QQ/网易云反爬开源选型调研并定案（§4.10）；② 全部开放问题定案（§7 决策记录）；③ 新增附录 A–G（迁移 DDL、API 契约、函数签名、前端规格、配置全表、测试矩阵、分阶段文件清单）。检查记录：

- **开源选型事实核查（2026-10 网络检索）**：Binaryify/NeteaseCloudMusicApi 已 DMCA 下架、生态迁移至 npm 社区包与 api-enhanced fork ✅；pyncm（weapi/eapi 签名）社区 fork 维护 ✅；qqmusic-api-python 在 PyPI 可用、repo 迁 GitCode ✅；163MusicLyrics（原 MusicLyricApp）Apache-2.0、活跃、含 QRC/KRC 解密与翻译合并 ✅——Apache-2.0 与本项目 MIT 兼容，引用需附 NOTICE（已写入 §4.10.1）。
- **代码事实核对（附录据此编写）**：`SCHEMA_VERSION=15`（下版迁移从 16 起）✅；`rules` 表 UNIQUE(scope,target,action)、`staged_lyrics`/`tasks` 列结构 ✅；`audio_index` 已有 `recording_id`（身份层可直接引用）✅；`NetEaseProviderConfig` 已有 `base_url`、`QQProviderConfig` 无（需新增）✅；`apply_cover_edit` 硬编码 FLAC、`CoverEditPlan/Outcome` 签名 ✅；`providers/qq.py` 的 subcode=-10002 语义与 `netease.py` 的自建网关路径换算为代码原文事实 ✅。
- **一致性检查**：§4.10 与风险表、§6 Phase 2、附录 G Phase 2 相互引用一致 ✅；翻译默认值跨处统一（§4.3、§5.4、§7 决策 2、附录 E = 新装 `bilingual_lrc` / 升级 `off`）✅；发现并修正一处——附录 E 初稿默认值列仍写 `off`，已改为按新装/升级分列。
- **API 契约检查**：手动歌词编辑并入既有 `POST /api/v1/tracks/apply-lyrics`（text 模式）而非新开重复端点；封面搜索命名 `cover/candidates` 避免与既有 `files/cover*` 混淆；todo 聚合复用 `/api/v1/issues` 查询口径——均避免与现有约 100 个端点的语义冲突。

**结论**：五轮检查共发现并修正约 29 处（自检 7 处 + 独立评审约 20 处 + 增量修订 2 处）。方案与代码现状、仓库硬约束（`AGENTS.md`）逐条对齐，开放问题全部定案，规格达到可直接开发的粒度，可作为实施依据。
