# Lyra 开发说明

本文件给接手代码的人和编码代理使用。用户文档见 [README.md](README.md) 和 [docs/](docs/index.md)。

## 项目

Lyra 是一个自托管的音乐库维护工具：歌词、标签、目录整理、重复检测。两个进程共用同一个镜像：

- `lyra serve`：FastAPI + Vue 前端。
- `lyra watch`：文件监听、对账、自动扫描、重复检测。

数据在 SQLite（WAL），曲库是用户的文件，Lyra 只做就地维护。

## 常用命令

```bash
# 后端
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .

# 前端
cd web && npx vitest run
cd web && npx vue-tsc --noEmit -p tsconfig.json
cd web && npm run build

# 本地运行
./.venv/bin/lyra config init
./.venv/bin/lyra config check
./.venv/bin/lyra serve

# 文档
./.venv/bin/pip install -r docs/requirements.txt
mkdocs build --strict
```

前端构建产物写入 `src/lyra/web/`，该目录不入库。只改 `web/`，不要手改产物。

## 代码地图

| 模块 | 职责 |
| --- | --- |
| `api.py` | HTTP 接口、后台任务、静态资源 |
| `cli.py` | 命令行入口 |
| `config.py` | 配置解析、校验、密钥掩码 |
| `db.py` | schema、迁移、写租约、事务 |
| `scan.py` / `audit.py` | 发现、配对、问题码 |
| `pipeline.py` | 抓取、匹配、暂存、写入编排 |
| `match.py` | 候选评分、时长闸门、版本判断 |
| `providers/` | QQ、网易云、LRCLIB |
| `tagsources/` | MusicBrainz 查询与字段比较 |
| `tagscrape.py` / `tagwrite.py` | 标签提案、写入、撤销 |
| `writer.py` + `*meta.py` | 字节级写入、备份、回滚 |
| `organize.py` | 路径生成、移动、空目录、撤销 |
| `dedupe.py` | 音频索引、分类、删除 |
| `fingerprint.py` | Chromaprint / AcoustID |

## 硬约束

- **禁止 `mutagen.save()`。** 写音频必须做字节级改写，保留非目标数据，写入后校验音频哈希。
- **路径必须经过 `_safe_root_file`。** 拒绝绝对路径、`..` 和符号链接逃逸。
- **新写接口必须套写租约。** 人工优先，等监控让出，不要直接报「已有任务」。
- **移动/删除先落库预览。** 目标存在绝不覆盖；删除进回收站，记录批次，支持撤销。
- **重复分类必须互斥。** 精确组、同一录音组、相似组不传递合并；参照项不参与删除。
- **冲突不自动覆盖。** 标签自动写入只补空和按可信目录纠正发行级字段。
- **数据库迁移版本 +1。** 迁移前备份，跨进程文件锁已由 `db.migrate()` 处理。
- **前端只改 `web/`。** 枚举值通过 `web/src/utils/labels.ts` 映射；文案中文、短句。
- **不提交** `lyra.toml`、`.env`、`data/`、测试曲库、构建产物、凭据。

## 测试

- 后端：`tests/`，pytest。真实素材缺失时 skip，不失败。
- 前端：`web/src/utils/__tests__/`，vitest。
- 路由/产物守卫：`tests/test_web_app.py`。
- 部署资产：`tests/test_deploy_assets.py`。
- 新增 API/页面/来源要同时补测试和文档。
- 涉及写入的测试只在 `tmp_path` 副本上做，不碰真实曲库。

## 排障顺序

1. `docker compose ps` 与 `docker compose logs`。
2. `/api/v1/health`。
3. `lyra status`。
4. `lyra config check`。
5. 数据库备份和迁移版本。
6. 具体任务的事务记录与备份。

常见边界：

- MusicBrainz 公共 API 每秒最多一次请求；自建镜像可降低间隔。
- 反向代理要传 `X-Forwarded-Proto`，否则 Cookie 的 Secure 判断失效。
- 扫描默认不跟随符号链接、不跨挂载点。
- SQLite 不要放在 SMB/NFS。
- 监听不可用时会降级为轮询，不会把全库当成已删除。

## 发布

1. 更新 `pyproject.toml`、`src/lyra/__init__.py`、`web/package.json` 版本。
2. 更新 `CHANGELOG.md`。
3. 提交并打 tag `vX.Y.Z`。
4. GitHub Actions 运行 CI、构建多架构镜像、发布 Release、部署文档站。
