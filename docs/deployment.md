# 部署与运维

## 组件

```text
浏览器 / 脚本
      │ HTTPS
      ▼
反向代理（Nginx / Caddy / Traefik）
      │ 127.0.0.1:8080
      ▼
┌──────────────┐      ┌──────────────┐
│ lyra         │      │ lyra-watch   │
│ FastAPI + UI │      │ 监控 / 扫描  │
└──────┬───────┘      └──────┬───────┘
       └──────────┬──────────┘
                  ▼
          SQLite（WAL）
          data/lyra.db
```

两个容器使用同一个镜像，共用 SQLite。Web 提供 API 和前端，watch 负责监听、对账、自动扫描和每日完整核对。

## Compose

根目录的 `compose.yaml` 是通用部署文件：

```bash
cp .env.example .env
mkdir -p config data
cp deploy/lyra.container.toml.example config/lyra.toml
docker compose up -d
```

默认只发布到 `127.0.0.1:8080`。修改 `LYRA_PORT` 可以换端口，但不要直接改成 `0.0.0.0` 暴露到公网。

使用发布镜像时，`LYRA_VERSION` 指定版本。使用本地源码构建：

```bash
docker compose build
docker compose up -d
```

构建参数：

| 参数 | 默认 | 用途 |
| --- | --- | --- |
| `NODE_IMAGE` | `node:22-alpine` | 前端构建镜像 |
| `NPM_REGISTRY` | `https://registry.npmjs.org` | npm 源 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | PyPI 源 |
| `PIP_TRUSTED_HOST` | 空 | 自签或镜像源的 trusted-host |
| `DEBIAN_MIRROR_HOST` | 空 | Debian 源主机名（如 `mirrors.aliyun.com`）；只换主机名，路径不变 |

网络受限时可以在 `.env` 中覆盖，例如：

```dotenv
NPM_REGISTRY=https://registry.npmmirror.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
DEBIAN_MIRROR_HOST=mirrors.aliyun.com
```

## 反向代理

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name music.example.com;

    ssl_certificate     /etc/ssl/music.example.com.pem;
    ssl_certificate_key /etc/ssl/music.example.com.key;

    client_max_body_size 0;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

`X-Forwarded-Proto` 必须传，否则会话 Cookie 的 `Secure` 判断会出错。

### Caddy

```caddyfile
music.example.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

Caddy 会自动申请证书并传递 forwarded 头。

### Traefik

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.lyra.rule=Host(`music.example.com`)
  - traefik.http.routers.lyra.entrypoints=websecure
  - traefik.http.routers.lyra.tls.certresolver=le
  - traefik.http.services.lyra.loadbalancer.server.port=8080
```

## 权限

容器默认以 `1000:1000` 运行。通过 `.env` 调整：

```dotenv
PUID=1000
PGID=1000
```

要求：

- `PUID:PGID` 对曲库有读写权限；
- 对 `data/` 和 `config/` 有读写权限；
- 不要在曲库内运行容器用户无法写入的 ACL。

检查：

```bash
docker compose exec lyra id
docker compose exec lyra touch /music/.lyra-write-test
docker compose exec lyra rm /music/.lyra-write-test
```

第二条命令只在确认曲库路径正确后执行。

## 只读审计

第一次部署建议先只读：

```toml
[[roots]]
read_only = true
```

或把挂载改为 `:ro`。然后：

```bash
docker compose exec lyra lyra audit
docker compose exec lyra lyra status
```

确认无误后再改回可写。

## 备份

需要备份：

- `config/lyra.toml`（含凭据，注意权限）；
- `data/lyra.db` 及 `-wal`、`-shm`；
- `data/backups/`（如果保留完整备份）；
- `data/trash/`（如果还需要还原）。

推荐在停止容器后备份，避免 SQLite WAL 不一致：

```bash
docker compose stop
tar -czf lyra-data-$(date +%F).tar.gz config data
docker compose start
```

在线备份可以先执行 SQLite checkpoint，再复制：

```bash
docker compose exec lyra python - <<'PY'
import sqlite3
conn = sqlite3.connect('/data/lyra.db')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
PY
```

不要只复制 `lyra.db` 而忽略 `-wal`。

## 升级

```bash
docker compose pull
docker compose up -d
```

数据库迁移在启动时执行，迁移前会自动备份。跨大版本升级前先读 [更新记录](changelog.md)。

升级不会改动曲库文件。正在运行的任务会中断，并在重启后标记为 interrupted；未完成事务会按文件哈希恢复。

## 健康检查

```bash
curl -s http://127.0.0.1:8080/api/v1/health
```

返回：

```json
{"ok":true,"version":"1.1.0","schema_version":12,"write_enabled":true,"busy":false}
```

`busy: true` 表示有长任务在运行，不代表故障。

## 日志

```bash
docker compose logs -f lyra
docker compose logs -f lyra-watch
```

Compose 默认限制每个容器 50 MB × 3 个文件。需要更长的审计记录时，用日志页或数据库，不要依赖容器 stdout。

## 自建 MusicBrainz 与网易云 API

`deploy/compose.yaml` 是高级部署示例，接入了：

- 外部 `musicbrainz_default` 网络；
- 可选的 `binaryify/netease_cloud_music_api` 服务。

使用前确保 MusicBrainz 网络已存在：

```bash
docker network ls | grep musicbrainz
```

网络名不同时：

```dotenv
MUSICBRAINZ_NETWORK=your_network_name
```

启用自建网易云 API：

```bash
docker compose -f deploy/compose.yaml --profile netease up -d
```

并在 `lyra.toml` 中设置：

```toml
[providers.netease]
base_url = "http://netease-api:3000"
```

不需要自建服务时，使用根目录的 `compose.yaml` 即可。

## 时间与时区

`TZ` 影响日志、每日完整核对和界面时间显示。默认 `Asia/Shanghai`。跨时区部署时同时修改 `[watch].timezone` 和容器 `TZ`。

## 资源建议

| 曲库规模 | 内存 | 说明 |
| --- | --- | --- |
| < 5,000 首 | 1 GB | 足够 |
| 5,000–20,000 首 | 2 GB | 首次统计和标签扫描更稳 |
| > 20,000 首 | 4 GB | 建议自建 MusicBrainz 镜像，分批扫描 |

磁盘空间主要被备份和回收站占用。定期检查 `data/backups/` 和 `data/trash/`。

## 文档站

文档位于 `docs/`，由 MkDocs Material 构建：

```bash
python -m pip install -r docs/requirements.txt
mkdocs serve
```

推送到 `main` 后，`.github/workflows/docs.yml` 会构建并推送到 `gh-pages` 分支。首次使用需要在仓库 Settings → Pages 中把 Source 设为 **Deploy from a branch**，分支选 `gh-pages`，地址为：

```text
https://astralwaveorg.github.io/lyra/
```

本地构建检查：

```bash
mkdocs build --strict
```

## 仓库设置

建议在 GitHub 仓库 Settings 中设置：

- Description：`Self-hosted lyrics, tags, and library organization for a local music collection`
- Topics：`music`、`lyrics`、`musicbrainz`、`self-hosted`、`nas`、`docker`
- Social preview：上传 `docs/assets/social-preview.png`
- Pages Source：Deploy from a branch，分支 `gh-pages`
- Discussions：可选，用于用法问答；安全问题仍走 Security Advisories

这些设置不在代码里，克隆仓库后需要手动配置一次。

## 镜像发布

打 tag 后，`.github/workflows/docker.yml` 会构建 `linux/amd64` 和 `linux/arm64` 镜像并推送到：

```text
ghcr.io/astralwaveorg/lyra
```

首次发布后，在 GitHub 组织的 Packages 设置中把镜像可见性改为 **Public**，否则 `docker compose pull` 需要登录 GHCR。发布标签包括完整版本、主次版本、主版本、`latest` 和 `sha-<commit>`。

本地构建不依赖 GHCR：

```bash
docker build -t lyra:local .
```
