# 部署文件

日常部署使用仓库根目录的 [`compose.yaml`](../compose.yaml)。本目录保留高级部署示例和 systemd 单元。

## 目录

| 文件 | 用途 |
| --- | --- |
| `compose.yaml` | 带自建 MusicBrainz 网络和可选网易云 API 的高级栈 |
| `.env.example` | 高级栈的环境变量模板 |
| `lyra.container.toml.example` | 容器内配置模板 |
| `lyra.service` | systemd 单元示例 |

通用安装步骤见[安装文档](../docs/installation.md)，反向代理和运维见[部署文档](../docs/deployment.md)。

## 高级栈

```bash
cp deploy/.env.example deploy/.env
mkdir -p config data
cp deploy/lyra.container.toml.example config/lyra.toml
docker compose -f deploy/compose.yaml up -d
```

需要：

- 一个已存在的 `musicbrainz_default` 外部网络，或通过 `MUSICBRAINZ_NETWORK` 指定；
- 可选的 `--profile netease` 启动自建网易云 API。

如果不需要自建 MusicBrainz，直接用根目录的 `compose.yaml`，它默认访问公共 MusicBrainz API。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `LIBRARY_HOST_PATH` | 宿主机曲库路径 |
| `CONFIG_HOST_DIR` | 放 `lyra.toml` 的目录 |
| `DATA_HOST_PATH` | 数据库、备份、回收站目录 |
| `PUID` / `PGID` | 运行身份 |
| `LYRA_PORT` | 宿主机监听端口 |
| `LYRA_VERSION` | 镜像版本 |
| `MUSICBRAINZ_NETWORK` | MusicBrainz 所在的 Docker 网络 |
| `LYRA_ADMIN_PASSWORD` | 管理员密码，留空则首次随机生成 |

## systemd

```bash
sudo useradd --system --uid 1000 --gid 1000 lyra
sudo mkdir -p /opt/lyra /etc/lyra /var/lib/lyra
sudo chown -R 1000:1000 /opt/lyra /etc/lyra /var/lib/lyra

sudo python3 -m venv /opt/lyra/.venv
sudo /opt/lyra/.venv/bin/pip install /path/to/lyra
sudo cp deploy/lyra.container.toml.example /etc/lyra/lyra.toml
sudo editor /etc/lyra/lyra.toml
sudo chmod 600 /etc/lyra/lyra.toml

sudo cp deploy/lyra.service /etc/systemd/system/lyra.service
sudo systemctl daemon-reload
sudo systemctl enable --now lyra
```

按实际曲库修改 `ReadWritePaths` 和 `lyra.toml` 的路径。
