# 安装与升级

Lyra 提供三种运行方式：Docker Compose、源码运行和 systemd。绝大多数用户应使用 Docker Compose。

## 环境要求

- Docker 24 或更新版本，Docker Compose v2。
- 一个可读写的音乐目录。只做审计时可以只读挂载。
- 至少 1 GB 内存；扫描大曲库时建议 2 GB 以上。
- 能访问 MusicBrainz、LRCLIB 等来源的网络。QQ 音乐和网易云的可用性受地区与网络影响。

镜像内已包含 `fpcalc`（Chromaprint），用于 AcoustID 指纹。不需要额外安装 ffmpeg。

## Docker Compose

### 1. 取得代码

```bash
git clone https://github.com/astralwaveorg/lyra.git
cd lyra
```

只想使用发布镜像时，也可以只下载 `compose.yaml`、`.env.example` 和配置模板。

### 2. 准备目录

```bash
mkdir -p config data
cp deploy/lyra.container.toml.example config/lyra.toml
```

目录结构：

```text
lyra/
├── compose.yaml
├── .env
├── config/
│   └── lyra.toml
├── data/
│   ├── lyra.db
│   ├── backups/
│   └── trash/
└── ...
```

`data/` 必须在曲库之外。不要把数据库、备份或回收站放进音乐目录，否则会被扫描器当成曲库内容。

### 3. 配置

复制环境变量模板：

```bash
cp .env.example .env
```

至少修改：

```dotenv
LIBRARY_HOST_PATH=/path/to/your/music
```

如果曲库在 NAS 上，使用宿主机绝对路径，例如 `/volume1/music`、`/mnt/user/music` 或 `/srv/music`。不要写容器内路径。

`config/lyra.toml` 使用容器内路径：

```toml
[[roots]]
name = "library"
role = "library"
path = "/music"
read_only = false
enabled = true

[storage]
data_dir = "/data"
```

### 4. 启动

```bash
docker compose up -d --build
```

首次启动会构建或拉取镜像，并初始化数据库。检查状态：

```bash
docker compose ps
docker compose logs -f lyra
curl -s http://127.0.0.1:8080/api/v1/health
```

健康检查返回 `ok: true` 即表示 Web 与数据库已就绪。

### 5. 首次登录

默认管理员用户名是 `admin`。如果没有通过环境变量或配置文件提供密码，Lyra 会生成随机密码并写入：

```bash
cat data/initial-password.txt
```

登录后立即在「设置 → 修改密码」中修改。也可以删除该文件，之后不会再生成。

如果希望首次启动就使用固定密码，在 `.env` 中设置：

```dotenv
LYRA_ADMIN_USERNAME=admin
LYRA_ADMIN_PASSWORD=换成至少 10 位的强密码
```

密码会以 scrypt 哈希存入数据库，明文不会写入日志。

Lyra 不提供固定默认密码（例如 `admin/Admin123456`）。固定口令一旦公开，等同于没有密码；随机初始密码或环境变量注入更安全。

### 6. 先做只读审计

建议第一次先不写入。把 `config/lyra.toml` 中的 `read_only` 设为 `true`，或者把 compose 的曲库挂载改成 `:ro`：

```yaml
- "${LIBRARY_HOST_PATH}:/music:ro"
```

然后运行：

```bash
docker compose exec lyra lyra audit
docker compose exec lyra lyra status
```

确认配对、歌词状态和问题码符合预期后，再改回可写。

## 从源码运行

需要 Python 3.11 或更新版本，以及 Node.js 22 用于构建前端。

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

cd web
npm ci
npm run build
cd ..

./.venv/bin/lyra config init
./.venv/bin/lyra config check
./.venv/bin/lyra serve
```

默认监听 `127.0.0.1:8080`。前端产物写入 `src/lyra/web/`，该目录不入库。

修改前端后重新构建：

```bash
cd web
npm run build
```

开发模式：

```bash
cd web
npm run dev
```

Vite 监听 `127.0.0.1:5273`，`/api` 代理到 `127.0.0.1:8080`。

## systemd

```bash
sudo useradd --system --uid 1000 --gid 1000 lyra
sudo mkdir -p /opt/lyra /etc/lyra /var/lib/lyra
sudo chown -R 1000:1000 /opt/lyra /etc/lyra /var/lib/lyra

sudo python3 -m venv /opt/lyra/.venv
sudo /opt/lyra/.venv/bin/pip install /path/to/lyra

sudo cp deploy/lyra.container.toml.example /etc/lyra/lyra.toml
sudo editor /etc/lyra/lyra.toml

sudo cp deploy/lyra.service /etc/systemd/system/lyra.service
sudo systemctl daemon-reload
sudo systemctl enable --now lyra
```

`deploy/lyra.service` 默认使用 `/srv/music` 和 `/var/lib/lyra`，按实际路径修改 `ReadWritePaths` 和 `lyra.toml`。

后台监控是独立进程：

```bash
sudo cp deploy/lyra.service /etc/systemd/system/lyra-watch.service
sudo sed -i 's|lyra serve|lyra watch|' /etc/systemd/system/lyra-watch.service
sudo systemctl enable --now lyra-watch
```

## NAS 注意事项

- **路径权限。** 容器以 `PUID:PGID` 运行。先用 `id` 确认目标用户在曲库上有读写权限。
- **数据目录。** 放在曲库之外，最好和曲库同一存储卷，避免备份和回收站跨卷。
- **SMB/NFS。** SQLite 的 WAL 模式不适合放在 SMB/NFS 共享上。数据库应放在本地磁盘。
- **符号链接。** 扫描默认不跟随符号链接，也不跨挂载点。需要管理多个目录时，给每个目录配置一个 `[[roots]]`。
- **资源。** 首次全库扫描会读取所有音频元数据；大曲库建议在空闲时段进行。

## 升级

升级前备份配置、数据库和数据目录：

```bash
docker compose stop
tar -czf lyra-backup-$(date +%F).tar.gz config data
docker compose pull
docker compose up -d
```

数据库迁移在启动时自动执行，迁移前会生成备份。跨多个大版本升级时，先读 [更新记录](changelog.md) 的迁移说明。

镜像标签使用具体版本，例如：

```dotenv
LYRA_VERSION=1.1.0
```

不要在生产环境长期使用 `latest`。

## 卸载

容器删除不会删除挂载目录：

```bash
docker compose down
```

确认不再需要数据后，再手动删除 `config/`、`data/` 和镜像。曲库本身不会被 Lyra 删除；如果用过回收站，确认 `data/trash/` 已清空。
