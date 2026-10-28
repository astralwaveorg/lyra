# Lyra 开发说明

Lyra 是自托管的音乐库维护工具：扫描本地曲库，补歌词/标签、整理目录、查重。`lyra serve`（FastAPI + Vue）和 `lyra watch`（监听、对账、自动任务）共用同一个镜像和 SQLite（WAL）；曲库文件只做就地维护。用户文档见 [README.md](README.md) 和 [docs/](docs/index.md)。

## 常用命令

```bash
# 依赖安装（Node 22+）
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cd web && npm ci && npm run build && cd ..

# 后端
./.venv/bin/python -m pytest -q
./.venv/bin/python -m pytest tests/test_match.py -q          # 单文件；::test_name 跑单条
./.venv/bin/ruff check .

# 前端
cd web && npm run test
cd web && npm run typecheck
cd web && npm run build

# 本地运行（新克隆先 lyra config init；已有 lyra.toml 时不要 --force）
./.venv/bin/lyra config check
./.venv/bin/lyra serve          # http://127.0.0.1:8080
cd web && npm run dev            # http://127.0.0.1:5273，/api 代理到 8080

# 文档
./.venv/bin/pip install -r docs/requirements.txt
mkdocs build --strict
```

提交前按 CI 顺序：先构建前端，再 `ruff check` → `pytest`；前端侧 `typecheck` → `test` → `build`。`Makefile` 有 `install/test/lint/typecheck/web-build/docs` 快捷目标，但 `make test` 不会先构建前端。

## 构建与产物

- 前端源码只在 `web/`。`npm run build` 直接写入 `src/lyra/web/`（不是 `web/dist`），该目录被 gitignore；不要手改产物。
- 后端测试 `tests/test_web_app.py` 会读取 `src/lyra/web/index.html` 和视图分包。新克隆或改前端后必须**先 build 再 pytest**；CI 也是这个顺序。
- 打包依赖 `pyproject.toml` 的 `artifacts = ["src/lyra/web/**"]`。删掉它，wheel 会漏掉前端；`tests/test_web_app.py` 会检查。

## 代码地图

完整数据流和更多模块见 [docs/architecture.md](docs/architecture.md)。

| 模块 | 职责 |
| --- | --- |
| `api.py` | FastAPI 路由、后台任务、静态资源；`manual_lease` 与 `_safe_root_file`/`_resolve_within_root` 都在这里 |
| `cli.py` | `serve`、`watch`、`status`、`audit`、`fetch`、`apply`、`tags` 等入口 |
| `config.py` / `models.py` | 配置解析、校验、密钥掩码；领域枚举与数据结构 |
| `db.py` | schema、迁移、写租约、事务 |
| `scan.py` / `audit.py` | 发现、配对、问题码 |
| `watcher.py` | 监听、稳定判定、周期对账、自动任务 |
| `pipeline.py` / `match.py` | 抓取 → 匹配 → 暂存；评分、时长/版本闸门 |
| `providers/` / `tagsources/` | 歌词源 / MusicBrainz |
| `tagscrape.py` / `tagwrite.py` | 标签提案、写入、撤销 |
| `writer.py` + `flacmeta.py`/`id3meta.py`/`oggmeta.py` | 字节级写入、备份、回滚 |
| `organize.py` / `dedupe.py` / `rename.py` / `fileops.py` | 整理、查重、改名、回收站 |
| `compare.py` | 候选/对比接口；两个“候选”结构不同，别混用类型 |
| `web/` | Vue 3 源码；产物写到 `src/lyra/web/` |

## 硬约束

- **禁止 `mutagen.save()`。** 写音频必须走 `writer.py` 的字节级改写，保留音频、封面、歌词和未知标签；写入后校验音频哈希，失败回滚。
- **路径必须先过安全解析。** 预览用 `_safe_root_file`，删除/替换等任意文件用 `_resolve_within_root`；两者都拒绝绝对路径、`..` 和符号链接逃逸。移动/删除目标存在时绝不覆盖。
- **新写接口套 `@manual_lease()`。** 人工优先：等监控让出写租约，不要直接返回“已有任务”。
- **移动/删除先落库预览。** 删除进回收站，记录批次并支持撤销。
- **重复分类必须互斥。** 精确组、同一录音组、相似组不传递合并；参照项不参与删除。
- **冲突不自动覆盖。** 标签自动写入只补空，并按可信目录纠正发行级字段。
- **人工痕迹不得被扫描覆盖。** 标签提案的 `ignored`（人工忽略）、`done`（人工已定案）与 `source='link'`（人工指定的来源）是人的判断，`upsert_tag_proposal` 不得用自动扫描结果改写它们；人工写入完成后把提案标成 `done` 而不是删行——删行等于把人的判断一起抹掉，下一轮扫描算出的匹配只要不同，这条曲子就会又回到队列。
- **判定归属只认「同一首」。** 歌词归属的判定不能用「文件名包含关系」：`01 Love Me Do.lrc`（规范化后 `lovemedo`）会命中 `1-16 - ME!.flac`（`me`），那是另一首歌。只认名字完全相同、或长的是短的**前缀/后缀**（「艺人 - 歌名」「歌名 - 版本」）；中间子串只能当提示，不能参与「是否多余」的判定——批量删除默认勾选的项必须是唯一解。「纯音乐，请欣赏」这类占位文案**不算已有歌词**，内嵌歌词**算**。
- **数据库迁移版本 +1。** 迁移前备份，跨进程文件锁由 `db.migrate()` 处理；同步更新 `tests/test_db.py` 和 `tests/test_fresh_migration.py`。
- **前端只改 `web/`。** 枚举值通过 `web/src/utils/labels.ts` 映射；文案中文、短句、纯文本，不写 Markdown 或内部术语。
- **Python 目标 3.11。** CI 跑 3.11/3.12；不要用 3.12+ 语法。仓库只跑 `ruff check`，没有配置格式化步骤，别顺手重排无关代码。
- **扫描边界不要放宽。** 默认不跟随符号链接、不跨挂载点；监听不可用时 watch 降级轮询，不能把全库标成已删除。
- **不提交** `lyra.toml`、`.env`、`data/`、测试曲库、`src/lyra/web/` 产物或凭据；本机这些目录是运行数据，不要删改。

## 测试与守卫

- 后端测试在仓库根跑；真实音频夹具由 `LYRA_TEST_LIBRARY` 指向测试库，缺失时 skip。写入/移动测试只用 `tmp_path` 副本，绝不能碰真实曲库。`pyproject.toml` 把 `DeprecationWarning` 当错误。
- 前端 vitest 只收集 `web/src/**/*.spec.ts`，环境是 `node`；不要写 `.test.ts`，依赖 DOM 的测试要自己声明环境。
- 改前端/路由/枚举时注意 `tests/test_web_app.py`：先 build；新增 `IssueCode` 或 `LyricsState` 必须在 `labels.ts` 有中文标签；Vue 模板不能出现 `sidecar`、`provider`、`pipeline` 等内部术语或 Markdown 标记。
- 改配置键要同步 `config.py` 的键白名单、`src/lyra/templates/lyra.example.toml`、`deploy/lyra.container.toml.example` 和 docs；`tests/test_templates.py` 会加载两份模板，未知键会被拒绝。
- 用户可见文案和 YAML 注释保持纯文本；`tests/test_ui_copy.py`、`tests/test_deploy_assets.py` 会拦 Markdown，`.dockerignore` 也有守卫。
- 新增 API/页面/歌词来源同时补测试和文档。

## 发版与协作

- 版本号散落在 `pyproject.toml`、`src/lyra/__init__.py`、`web/package.json`、`web/package-lock.json`、`CITATION.cff`、`compose.yaml`、`deploy/compose.yaml`、`.env.example`、`deploy/.env.example` 和 `docs/index.md`；先用 `rg '<当前版本>'` 找全，再更新 `CHANGELOG.md`。
- 提交后打 `vX.Y.Z` tag。Release workflow 创建 Release；Docker workflow 构建多架构镜像；Docs workflow 在 `main` 的 docs 变更后部署文档站。
- 提交/PR 约定见 [CONTRIBUTING.md](CONTRIBUTING.md)：从 `main` 开分支，提交信息中文、动词开头、一事一提交。

## 运行时排障

`docker compose ps/logs` → `/api/v1/health` → `lyra status` → `lyra config check` → 数据库备份/迁移版本。不要为了排障删除 `data/` 或重建库。
