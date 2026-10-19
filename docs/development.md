# 开发

## 环境

- Python 3.11+
- Node.js 22+
- 可选：`fpcalc`（Chromaprint），用于指纹测试

```bash
git clone https://github.com/astralwaveorg/lyra.git
cd lyra

python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

cd web
npm ci
npm run build
cd ..

cp src/lyra/templates/lyra.example.toml lyra.toml
./.venv/bin/lyra config check
```

`lyra.toml` 已被 `.gitignore` 排除，可以放本地路径和测试凭据。

## 运行

后端：

```bash
./.venv/bin/lyra serve
```

前端开发：

```bash
cd web
npm run dev
```

Vite 监听 `127.0.0.1:5273`，`/api` 代理到 `127.0.0.1:8080`。修改前端后不需要手动构建；生产运行前再执行 `npm run build`。

## 测试

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .

cd web
npx vitest run
npx vue-tsc --noEmit -p tsconfig.json
npm run build
```

后端测试覆盖：

- 配置解析与未知键拒绝
- 歌词解析、匹配、闸门、负缓存
- FLAC / MP3 / OGG / Opus 字节级读写
- 写入事务、备份、回滚、崩溃恢复
- 标签提案、冲突、撤销
- 整理预览、移动、空目录、删除、撤销
- 重复分类纯度、参照项、批量删除校验
- 鉴权、首次启动、API 契约
- 前端产物、路由回退、静态资源缓存

依赖真实音频的测试在素材缺失时会 skip，不会失败。可以用 `LYRA_TEST_LIBRARY` 指向一个测试曲库：

```bash
LYRA_TEST_LIBRARY=/path/to/test/library ./.venv/bin/python -m pytest -q
```

不要在真实曲库上运行写入测试。测试只在临时目录里操作副本。

## 代码地图

见[架构](architecture.md)。常用入口：

| 任务 | 文件 |
| --- | --- |
| 加 API | `src/lyra/api.py`，同时补 `tests/test_*_api.py` |
| 加页面 | `web/src/views/`、`web/src/router/index.ts`、`web/src/api/` |
| 加歌词来源 | `src/lyra/providers/`，实现 `Provider` 协议 |
| 改标签字段 | `src/lyra/tagsources/`、`src/lyra/tagscrape.py`、`web/src/views/TagsScrapeView.vue` |
| 改写入 | `src/lyra/writer.py`、`src/lyra/flacmeta.py`、`src/lyra/id3meta.py`、`src/lyra/oggmeta.py` |
| 改数据库 | `src/lyra/db.py`，迁移版本 +1 |
| 改整理 | `src/lyra/organize.py`、`web/src/views/OrganizeView.vue` |
| 改重复 | `src/lyra/dedupe.py`、`tests/test_dedupe.py` |

## 约定

### 写入

- 不允许用 `mutagen.save()` 覆盖整份标签。
- 只改目标字段；音频、封面、歌词、未知标签保持不变。
- 写入后校验音频哈希；失败必须回滚。
- 新写文件的接口要套写租约 `@manual_lease()`。

### 路径

- 所有 `root + path` 接口必须经过 `_safe_root_file`，拒绝绝对路径、`..` 和符号链接。
- 移动目标存在时拒绝覆盖。
- 跨卷移动拒绝，不做隐式复制。

### 数据库

- 迁移写在 `db.py`，版本号只增不减。
- 迁移前备份，跨进程用文件锁。
- 新表要有索引和清理策略。
- 不要依赖数据库中的绝对路径做恢复；以磁盘实际文件为准。

### 前端

- 只改 `web/`，不要手改 `src/lyra/web/`。
- 文案中文、短句，不写解释性副标题，不用全角括号塞说明。
- 枚举值通过 `web/src/utils/labels.ts` 映射，不在模板里直接显示英文。
- 新页面在 `web/src/router/index.ts` 注册，并在 `tests/test_web_app.py` 的路由检查里补上。
- 桌面和移动端共用一套路由；窄屏下表格改为卡片。

### 注释

- 解释为什么这样做，不解释代码字面意思。
- 不写抓取过程、临时环境或会话记录；把结论写进文档或测试。
- 不引用内部规范编号；需要背景时链接到 `docs/`。

## 加一个歌词来源

1. 在 `src/lyra/providers/` 新建模块，实现 `search()`、`lyrics()`、`available`。
2. 在 `providers/base.py` 复用 `HttpProviderBase`、限流和熔断。
3. 在 `config.py` 增加配置段和键白名单。
4. 在 `providers/__init__.py` 注册工厂。
5. 补 fixture、单元测试和 API 契约测试。
6. 在 `docs/configuration.md` 和 `docs/usage.md` 说明。

不要在没有候选时返回空列表冒充「没有歌词」。上游结构变化、限流、网络错误要分别映射，避免污染负缓存。

## 数据库迁移

```python
_MIGRATION_13 = """
CREATE TABLE ...;
"""
MIGRATIONS = {..., 13: _MIGRATION_13}
```

- 版本号 +1。
- 迁移必须幂等或可重复执行。
- 先在空库和旧库副本上测试。
- 更新 `tests/test_fresh_migration.py` 和 `tests/test_db.py`。

## 发布

1. 更新 `pyproject.toml`、`src/lyra/__init__.py`、`web/package.json` 的版本。
2. 更新 `CHANGELOG.md`。
3. 提交并打 tag：

```bash
git tag -a v1.0.0 -m "Lyra 1.0.0"
git push origin v1.0.0
```

4. GitHub Actions 会运行测试、构建多架构镜像并推送到 GHCR。
5. 在 Releases 页面确认镜像标签和发布说明。

发布前检查：

- `pytest`、`vitest`、`vue-tsc`、`npm run build` 全绿。
- `ruff check .` 无错误。
- `mkdocs build --strict` 通过。
- 镜像能启动，`/api/v1/health` 返回 200。
- 没有提交 `lyra.toml`、`.env`、`data/` 或测试曲库。
