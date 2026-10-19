# Lyra

[![CI](https://github.com/astralwaveorg/lyra/actions/workflows/ci.yml/badge.svg)](https://github.com/astralwaveorg/lyra/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/astralwaveorg/lyra)](https://github.com/astralwaveorg/lyra/releases)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GHCR](https://img.shields.io/badge/ghcr.io-astralwaveorg%2Flyra-blue?logo=docker)](https://github.com/astralwaveorg/lyra/pkgs/container/lyra)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Lyra 是一个自托管的音乐库维护工具。它扫描本地曲库，补齐歌词和标签，按规则整理目录，检查重复音频，并把每一次写入、移动和删除都记进可撤销的记录里。

它面向这样的场景：曲库已经在 NAS 或本地磁盘上，文件以 FLAC、MP3、OGG 为主，标签和歌词参差不齐，但不希望交给某个云端服务批量改写。Lyra 默认只监听本机，所有写操作都可预览、可备份、可回滚。

![概览](docs/assets/screenshots/overview.png)

![重复音频分类与一键处理](docs/assets/screenshots/dedupe.png)

## 功能

**歌词**

- 递归扫描曲库，识别 FLAC、MP3、OGG Vorbis、Opus。
- 从 QQ 音乐、网易云音乐、LRCLIB 抓取歌词，按标题、艺人、专辑、时长和版本评分。
- 内嵌歌词与同名 `.lrc` 双向写入；没有 `.lrc` 时从内嵌歌词生成。
- 已有有效同步歌词默认跳过；纯文本、占位词、时间轴异常分别处理。
- 歌词审查页支持逐行对比、来源切换、批量采纳和撤销。

**标签**

- 以 MusicBrainz 为主源，默认使用公共 API，也可以接入自建镜像。
- 支持 MBID 查询、目录共识、标题搜索和 AcoustID 指纹兜底。
- 逐字段显示本地值、来源值和判断结果：补空、纠正、冲突、一致。
- 冲突字段默认勾选，可一键全选或取消；写入前二次确认。
- 自动监控按设置的间隔扫描，只自动补缺失字段，冲突永远留给人。
- 写入只改目标字段，音频数据、封面、歌词和未知标签保持不变。

**曲库整理**

- 按 `艺人 / 专辑 / 音轨号 - 曲名` 生成路径；支持多艺人、合辑、多碟和缺失字段降级。
- 范围可多选目录，也可多选排除目录；不会在后台自动移动。
- 所有移动先预览新旧路径，确认后执行；目标存在时绝不覆盖。
- `.lrc` 跟随音频改名；移动后为空的目录可以一并清理。
- 重复音频按证据分类：音频完全一致、同一录音、标题/艺人/时长相近。
- 分类过滤、分类全选、分类一键删除；参照项和其他分类的文件不会被连带删除。
- 删除是移入回收站，不是抹掉；单条或整批都能撤销。

**可追溯**

- 写入前备份原文件，记录被改字段的原值。
- 整理操作记录旧路径、新路径、回收站路径和批次。
- 日志页按写入、改名、整理分组，支持多选批量撤销。
- 崩溃后按实际文件哈希恢复未完成事务，不谎报成功。

## 快速开始

需要 Docker 24+ 与 Docker Compose v2。

```bash
git clone https://github.com/astralwaveorg/lyra.git
cd lyra

cp .env.example .env
mkdir -p config data
cp deploy/lyra.container.toml.example config/lyra.toml
```

编辑 `.env`，至少设置曲库路径：

```dotenv
LIBRARY_HOST_PATH=/path/to/your/music
```

编辑 `config/lyra.toml`，确认 `[[roots]].path` 是容器内路径。使用上面的 compose 时保持 `/music` 即可。

启动：

```bash
docker compose up -d --build
```

`--build` 从当前源码构建；也可以先 `docker compose pull`，再 `docker compose up -d` 使用发布镜像。

打开 `http://127.0.0.1:8080`。首次启动会创建管理员 `admin`，随机密码写入 `data/initial-password.txt`：

```bash
cat data/initial-password.txt
```

登录后请在「设置 → 修改密码」里改掉初始密码。也可以在 `.env` 里设置 `LYRA_ADMIN_PASSWORD`，让首次启动直接使用指定密码。

生产环境不要把端口直接暴露到公网。把 `127.0.0.1:8080` 交给 Nginx、Caddy 或 Traefik 做 HTTPS 反向代理，参见[部署文档](docs/deployment.md)。

## 配置

配置分三层，优先级从低到高：

1. 内置默认值
2. `lyra.toml`
3. 环境变量 `LYRA_*`

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `LIBRARY_HOST_PATH` | 宿主机曲库路径，Compose 使用 |
| `LYRA_CONFIG` | 配置文件路径，容器内默认 `/config/lyra.toml` |
| `LYRA_LIBRARY_ROOT` | 容器内曲库路径，默认 `/music` |
| `LYRA_DATA_DIR` | 数据目录，默认 `/data` |
| `LYRA_WEB_BIND` | 监听地址，默认 `127.0.0.1` |
| `LYRA_WEB_PORT` | 监听端口，默认 `8080` |
| `LYRA_ADMIN_USERNAME` | 管理员用户名，默认 `admin` |
| `LYRA_ADMIN_PASSWORD` | 管理员密码；留空则首次启动随机生成 |
| `LYRA_ACOUSTID_API_KEY` | AcoustID API key，可选 |
| `LYRA_AI_API_KEY` | AI 辅助判断的 API key，可选 |

完整配置项、MusicBrainz 镜像接法、反向代理示例和备份说明见：

- [安装与升级](docs/installation.md)
- [配置参考](docs/configuration.md)
- [部署与反向代理](docs/deployment.md)
- [常见问题](docs/troubleshooting.md)

## 安全边界

- 默认只监听回环地址，由反向代理对外提供 HTTPS。
- 单管理员账户，密码使用 scrypt 哈希；Web 用会话 Cookie，脚本和移动端用 Bearer Token。
- 凭据只从环境变量或 0600 配置文件读取，不写进日志、页面或导出。
- 写音频前完整备份，写入后校验音频哈希和非目标标签。
- 整理和删除先落库预览，目标存在绝不覆盖；删除进入回收站。
- 重复检测只读，不会自动删除任何文件。
- 只读模式 `policy.write_enabled = false` 下，所有写接口拒绝执行。

发现安全问题请走 [GitHub 私密安全通告](https://github.com/astralwaveorg/lyra/security/advisories/new)，不要公开提交 Issue。细节见 [SECURITY.md](SECURITY.md)。

## 支持格式

| 格式 | 读取 | 歌词写入 | 标签写入 | 封面写入 |
| --- | --- | --- | --- | --- |
| FLAC | 是 | 是 | 是 | 是 |
| MP3 | 是 | 是 | 是 | 是 |
| OGG Vorbis | 是 | 是 | 是 | 否 |
| Opus | 是 | 是 | 是 | 否 |

写入使用字节级改写，不调用 `mutagen.save()` 覆盖整份标签。写入前后会比较音频数据哈希；不一致即回滚。

## 开发

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

cd web
npm ci
npm run build
cd ..

./.venv/bin/lyra config init
./.venv/bin/lyra config check
./.venv/bin/python -m pytest -q
```

前端开发：

```bash
cd web
npm run dev      # http://127.0.0.1:5273，/api 代理到 8080
```

测试与贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md) 和[开发文档](docs/development.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/](docs/index.md) | 文档首页 |
| [安装](docs/installation.md) | Docker、源码、systemd、NAS |
| [配置](docs/configuration.md) | 全部配置项与环境变量 |
| [使用](docs/usage.md) | 各页面与典型工作流 |
| [标签刮削](docs/tags.md) | 匹配、冲突、自动写入与撤销 |
| [曲库整理](docs/organize.md) | 命名、重复分类、删除与撤销 |
| [部署](docs/deployment.md) | Compose、反向代理、权限、备份 |
| [架构](docs/architecture.md) | 组件、数据流与安全设计 |
| [API](docs/api.md) | 鉴权与主要接口 |
| [故障排查](docs/troubleshooting.md) | 常见错误与处理 |

## 交流与赞助

- 问题反馈：[GitHub Issues](https://github.com/astralwaveorg/lyra/issues)
- 交流群：见[赞助与社群](docs/sponsor.md)
- 赞赏：完全自愿，不影响任何功能

如果 Lyra 对你有帮助，欢迎点一个 Star，或者提交 Issue 和 Pull Request。代码、文档、翻译、部署经验都欢迎。

## 许可

MIT License，版权归 Lyra contributors。见 [LICENSE](LICENSE)。
