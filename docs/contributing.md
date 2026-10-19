# 贡献

完整的贡献指南在仓库根目录的 [CONTRIBUTING.md](https://github.com/astralwaveorg/lyra/blob/main/CONTRIBUTING.md)。

## 快速开始

```bash
git clone https://github.com/astralwaveorg/lyra.git
cd lyra
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cd web && npm ci && npm run build && cd ..

./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .
```

## 提交前

- 后端测试、前端测试、类型检查、构建全部通过。
- 新行为有测试。
- 文档已更新。
- 没有提交 `lyra.toml`、`.env`、`data/`、测试曲库或构建产物。
- 没有无关的格式化改动。

## 适合新贡献者的任务

- 补充部署示例（Caddy、Traefik、Unraid、群晖）。
- 改进错误提示和文档。
- 补充测试用例和边界样本。
- 移动端布局和可访问性改进。
- 英文文档和翻译。

## 不适合的改动

- 引入云端依赖，把曲库或歌词上传到第三方。
- 自动删除重复音频或后台自动移动文件。
- 用 `mutagen.save()` 覆盖整份标签。
- 为了让测试通过而放宽安全检查。

## 行为准则

参与项目即表示同意遵守 [CODE_OF_CONDUCT.md](https://github.com/astralwaveorg/lyra/blob/main/CODE_OF_CONDUCT.md)。
