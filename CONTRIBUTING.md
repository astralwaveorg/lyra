# 贡献指南

感谢愿意花时间改进 Lyra。无论是 Bug、文档、翻译、部署经验还是代码，都有帮助。

## 提交 Issue

- 先搜索已有 Issue，避免重复。
- Bug 请附 Lyra 版本、部署方式、复现步骤、预期结果和实际结果。
- 界面问题请附截图；移动端问题请注明屏幕尺寸。
- 日志请用代码块粘贴，不要附 API key、密码、数据库或完整曲库路径。
- 功能建议请说明使用场景，而不只是「加一个按钮」。

安全漏洞不要公开提交 Issue，走 [私密安全通告](SECURITY.md)。

## 提交 Pull Request

1. Fork 仓库，从 `main` 创建分支。
2. 修改代码，补测试。
3. 本地跑完检查。
4. 提交 PR，说明改了什么、为什么改、怎么验证。

分支名建议：

```text
fix/tag-write-rollback
feat/ogg-cover
docs/install-caddy
```

### PR 检查清单

- [ ] 后端测试通过
- [ ] 前端测试、类型检查、构建通过
- [ ] `ruff check .` 通过
- [ ] 新增行为有测试
- [ ] 文档已更新
- [ ] 没有提交凭据、数据库、曲库文件或构建产物
- [ ] 没有无关的格式化改动

## 本地检查

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .

cd web
npx vitest run
npx vue-tsc --noEmit -p tsconfig.json
npm run build
```

涉及文档时：

```bash
./.venv/bin/pip install -r docs/requirements.txt
mkdocs build --strict
```

## 代码约定

### Python

- 目标 Python 3.11。
- 行宽 100，ruff 规则见 `pyproject.toml`。
- 类型标注优先，避免无必要的 `Any`。
- 不引入重量级依赖；新增依赖需在 PR 里说明理由。
- 注释解释原因和边界，不写过程叙述。

### 文件写入

- 禁止 `mutagen.save()` 覆盖整份标签。
- 只改目标字段，保留音频、封面、歌词和未知标签。
- 写入后校验音频哈希，失败回滚。
- 新写接口套写租约，路径经过 `_safe_root_file`。

### 数据库

- 迁移版本 +1，写在 `db.py`。
- 迁移前备份，跨进程加锁。
- 新表补索引和清理策略。
- 同时更新 `tests/test_db.py`、`tests/test_fresh_migration.py`。

### 前端

- 只改 `web/`，不要改 `src/lyra/web/`。
- Vue 3 `<script setup>` + TypeScript。
- 文案中文、短句；不出现内部术语。
- 枚举值用 `labels.ts` 映射。
- 新页面补路由和路由测试。
- 窄屏下保证可用，不要只做桌面。

### 文档

- 中文为主；英文 README 单独维护。
- 命令可复制粘贴，路径用示例域名和示例目录。
- 不写抓取过程、临时环境或会话记录。
- 截图不要包含个人信息、真实曲库路径或凭据。

## 提交信息

使用简短、以动词开头的说明：

```text
修复标签写入回滚时未恢复备份
增加 OGG 封面写入
整理预览支持多选排除目录
```

避免：

```text
update
fix bug
修改了一下
```

一个提交只做一件事。格式化、重命名和功能改动尽量分开。

## 许可

提交代码即表示同意以 MIT 许可证发布你的贡献。不要提交来自其他项目的、许可证不兼容的代码。

## 行为准则

参与项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
