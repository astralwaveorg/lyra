# 配置参考

Lyra 的配置分为三层，优先级从低到高：

1. 内置默认值
2. `lyra.toml`
3. 环境变量 `LYRA_*`

未知键会直接报错。拼错一个键却静默使用默认值，比启动失败更难排查。

## 最小配置

```toml
config_version = 1

[[roots]]
name = "library"
role = "library"
path = "/music"
read_only = false
enabled = true

[storage]
data_dir = "/data"

[web]
bind = "0.0.0.0"
port = 8080
auth_required = true
```

容器部署时，`path` 和 `data_dir` 用容器内路径；宿主机路径由 Compose 的卷映射决定。

## 配置校验

```bash
lyra config check
lyra config check --json
```

`config check` 会打印生效值和被掩码的凭据。容器内：

```bash
docker compose exec lyra lyra config check
```

## 受管根 `[[roots]]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `name` | `main` | 根的稳定标识，出现在 API、日志和整理记录里 |
| `role` | `library` | `library` 就地管理；`inbox` 预留给后续版本 |
| `path` | 空 | 服务视角的绝对路径 |
| `host_path` | 空 | 仅用于提示，不参与逻辑 |
| `read_only` | `false` | 只读模式，拒绝写入该根 |
| `enabled` | `true` | 是否启用 |
| `ignore_dirs` | `[]` | 任意层级跳过的目录名 |
| `ignore_globs` | `[]` | 跳过的文件名 glob |

多个曲库配置多个 `[[roots]]`。扫描默认不跟随符号链接、不跨挂载点。

## 存储 `[storage]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `data_dir` | `./data` | 数据库、备份、暂存、回收站和日志 |

数据目录必须在曲库之外，且放在本地磁盘。SQLite WAL 不适合 SMB/NFS。

## 监听 `[web]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `bind` | `127.0.0.1` | 监听地址；容器内通常设为 `0.0.0.0` |
| `port` | `8080` | 监听端口 |
| `auth_required` | `true` | 是否要求登录 |
| `admin_username` | `admin` | 管理员用户名 |
| `admin_password` | 空 | 管理员密码；留空则首次启动随机生成 |

密码可以用环境变量提供，优先级高于配置文件：

```bash
LYRA_ADMIN_USERNAME=admin
LYRA_ADMIN_PASSWORD='至少 10 位的强密码'
```

配置文件里的密码应确保文件权限为 `0600`。

## 监控 `[watch]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `backend` | `auto` | `auto`、`inotify`、`fsevents` 或 `poll` |
| `stable_seconds` | `60` | 文件连续多久无变化才处理 |
| `stable_samples` | `3` | 至少采样次数 |
| `reconcile_interval_seconds` | `900` | 轻量对账间隔 |
| `full_scan_local_time` | `03:30` | 每日完整核对时间 |
| `timezone` | `Asia/Shanghai` | 调度使用的时区 |

监听不可用时会降级为轮询，并在界面上如实显示。

## 来源 `[providers]`

```toml
[providers]
enabled = ["qq", "netease", "lrclib"]
priority_zh = ["qq", "netease", "lrclib"]
priority_foreign = ["lrclib", "netease", "qq"]
cjk_uses_zh_priority = true
```

未列在 `enabled` 中的来源不会被使用。优先级按语种分开配置，避免用一套顺序处理所有曲目。

### QQ 音乐

```toml
[providers.qq]
minimum_request_interval_seconds = 2
query_limit_per_round = 3
candidates_per_query = 20
detail_and_lyric_limit_per_round = 6
max_inline_retries = 2
max_inline_retry_wait_seconds = 5
```

QQ 接口在部分网络环境下不可用，表现为 HTTP 500 或空结果。Lyra 会按失败类型区分，不会把网络故障当成「没有歌词」。

### 网易云音乐

```toml
[providers.netease]
minimum_request_interval_seconds = 1.5
query_limit_per_round = 3
candidates_per_query = 20
detail_and_lyric_limit_per_round = 6
base_url = ""
```

`base_url` 留空时直接使用 `music.163.com` 的公开接口。也可以指向自建 NetEaseCloudMusicApi，例如 `http://netease-api:3000`。自建版的端点路径不同，Lyra 会自动换算。

### LRCLIB

```toml
[providers.lrclib]
client_name = "Lyra"
client_version = "1.1.0"
client_url = "https://github.com/astralwaveorg/lyra"
authorization_ref = ""
minimum_request_interval_seconds = 0.5
max_backoff_seconds = 3600
circuit_breaker_threshold = 5
circuit_breaker_initial_seconds = 1800
```

LRCLIB 不要求 API key，但要求可识别的客户端标识。`authorization_ref` 是可选的授权 reference，用环境变量 `LYRA_LRCLIB_AUTHORIZATION_REF` 注入更安全。

### MusicBrainz

```toml
[providers.musicbrainz]
enabled = true
base_url = "https://musicbrainz.org"
minimum_request_interval_seconds = 1.1
request_timeout_seconds = 20
search_limit = 8
detail_limit = 3
```

默认使用公共 API，按官方限制每秒最多一次请求，因此间隔取 1.1 秒。

曲库较大时建议自建 MusicBrainz 镜像。镜像同样提供 `/ws/2`，把 `base_url` 指向容器地址，并把间隔降到 `0.2` 左右：

```toml
[providers.musicbrainz]
base_url = "http://musicbrainz:5000"
minimum_request_interval_seconds = 0.2
```

使用自建镜像时，`lyra` 和 `lyra-watch` 需要接入镜像所在的 Docker 网络。`deploy/compose.yaml` 提供了一个带 `musicbrainz_default` 外部网络的示例；`MUSICBRAINZ_NETWORK` 可以改网络名。

### AcoustID

```toml
[providers.acoustid]
enabled = false
api_key = ""
base_url = "https://api.acoustid.org"
minimum_request_interval_seconds = 0.34
```

启用前需要：

- 在 [AcoustID](https://acoustid.org/) 注册应用并取得 API key。
- 镜像内有 `fpcalc`，官方镜像已包含。
- 在设置页打开「指纹校验」，或在配置里设置 `policy.tags.fingerprint = true`。

指纹只用于缺 MBID 或候选有歧义的情况，不会对全库无条件计算。

## 网络代理 `[network]`

```toml
[network]
proxy = ""
direct_hosts = ["qq.com", "163.com", "126.net", "gtimg.cn"]
```

用于访问 LRCLIB、MusicBrainz、AcoustID、大模型和封面等外部服务。支持 `http://`、`https://`、`socks5://`、`socks5h://`，必须写明端口：

```toml
[network]
proxy = "http://10.0.1.8:7890"
# proxy = "socks5://10.0.1.8:7890"
```

设置页「网络代理」保存的值优先于配置文件。两者都没有指定时，沿用容器或系统的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。设置页保存到 `data/network.json`（权限 `0600`），`lyra` 与 `lyra-watch` 共用；监控进程约 30 秒内应用新值。

`direct_hosts` 里的主机即使开了代理也直连。默认是国内歌词来源与它们的封面站点：代理是给境外服务找路的，国内来源本来通，绕一圈只会多一跳，还可能因为出口 IP 变了被拒。改成空数组可以让所有请求都走代理。

回环地址、内网 IP、Docker 服务名，以及配置里指向内网的网易云 API / MusicBrainz 镜像会自动绕过代理，不受 `direct_hosts` 影响。

## 处理策略 `[policy]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `write_enabled` | `true` | 总写入开关；false 时全库只读 |
| `write_mode` | `auto` | `auto`、`off`、`verified_only` |
| `write_sidecar_lrc` | `true` | 写入同名 `.lrc` |
| `embed_lyrics` | `true` | 写入内嵌歌词 |
| `fill_empty_lrc` | `true` | 0 字节或纯空白 `.lrc` 视为不存在 |
| `skip_if_valid_synced` | `true` | 已有有效同步歌词时跳过 |
| `clear_plain_embedded` | `true` | 清理无时间轴内嵌歌词，只碰歌词字段 |
| `allow_other_providers` | `false` | 是否允许未声明的来源 |

### 匹配 `[policy.matching]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `max_duration_delta_seconds` | `5.0` | 时长闸门，超过直接否决 |
| `min_score_margin` | `0.15` | 最优与次优的最小分差 |
| `min_confidence` | `0.75` | 最低置信度 |
| `auto_accept_within_seconds` | `1.0` | 时长差在此范围内且分数达标时可直接接受 |

### 标签刮削 `[policy.tags]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `auto_scan` | `true` | 监控进程按间隔扫描 |
| `scan_interval_hours` | `24` | 扫描间隔，0.5 到 168 小时 |
| `auto_write` | `true` | 自动补缺失字段 |
| `fingerprint` | `true` | 缺 MBID 或有歧义时用 AcoustID |
| `max_auto_write_per_run` | `300` | 单轮最多自动写入多少首 |
| `max_duration_delta_seconds` | `5.0` | 标签自动写入的时长容差 |

设置页保存的策略写入数据库 `meta.settings.tags_policy`，启动时覆盖 `[policy.tags]`。修改配置文件不会覆盖设置页已经保存的值；需要以设置页为准，或在设置页重新保存。

## 歌词格式 `[lyrics]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `encoding` | `utf-8` | 输出编码 |
| `bom` | `false` | 是否写 BOM |
| `line_ending` | `lf` | `lf` 或 `crlf` |
| `format` | `line_lrc` | 行级 LRC |
| `translation` | `false` | 是否写翻译 |
| `romanization` | `false` | 是否写罗马音 |

## 写入 `[write]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `backup_before_write` | `true` | 写入前备份原文件 |
| `backup_review_only` | `true` | 只给需要人工确认的写入做完整备份 |
| `backup_auto_prune` | `false` | 是否自动淘汰旧备份 |
| `backup_max_bytes` | `0` | 备份容量上限，0 表示不限制 |
| `auto_clean_orphan_sidecars` | `false` | 自动清理确认无归属的 `.lrc` |

自动接受的低风险写入默认不做整份备份，因为大曲库整份复制代价很高。需要每次写入都备份时，把 `backup_review_only` 设为 `false`。

## 重试 `[retry]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `not_found_intervals_days` | `[7, 30, 90]` | 无合格候选后的复查间隔 |
| `no_synced_intervals_days` | `[30, 90, 180]` | 有候选但无同步歌词的复查间隔 |
| `transient_backoff_seconds` | `[60, 300, 1800, 7200, 21600]` | 临时网络错误退避 |

## 大模型 `[ai]`

默认关闭，只用于「两份歌词是不是同一首、哪份更完整」这类语义判断。

```toml
[ai]
enabled = false
protocol = "openai_chat"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key = ""
timeout_seconds = 30
max_requests_per_run = 12
auto_clean_duplicates = false
```

### 协议

| `protocol` | 请求 endpoint | 默认地址示例 |
| --- | --- | --- |
| `openai_chat` | `/chat/completions` | `https://api.openai.com/v1` |
| `openai_responses` | `/responses` | `https://api.openai.com/v1` |
| `anthropic` | `/v1/messages` | `https://api.anthropic.com` |

`base_url` 可以填 base URL，也可以填完整 endpoint。填完整地址时 Lyra 不再拼接路径：

```toml
# OpenAI 兼容服务
base_url = "https://api.deepseek.com"
model = "deepseek-chat"

# 完整 endpoint 也可以
base_url = "https://api.example.com/v1/chat/completions"

# Anthropic
protocol = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-4-5"
```

OpenAI 官方的 base URL 需要包含 `/v1`；其他兼容服务如果要求不同的路径，直接填完整 endpoint 最稳妥。

### 设置页

「设置 → 大模型」可以配置协议、API 地址、模型 ID、Token、超时和单轮请求上限，并提供「测试连接」按钮。保存后立即生效，不需要重启。

Token 的处理：

- 设置页保存的 Token 写入 `data/ai-token`，权限 `0600`，不进入数据库，也不会在接口里返回。
- 环境变量 `LYRA_AI_API_KEY` 优先级最高；设置页不能覆盖它。
- 配置文件里的 `api_key` 可以被设置页保存的 Token 覆盖；清除设置页 Token 后回退到配置文件。

即使模型判断可以清理，程序仍要求确定性的歌词行覆盖条件，并把文件移入回收站而不是删除。

## 环境变量

| 变量 | 对应配置 |
| --- | --- |
| `LYRA_CONFIG` | 配置文件路径 |
| `LYRA_LIBRARY_ROOT` | 第一个受管根的 `path` |
| `LYRA_DATA_DIR` | `storage.data_dir` |
| `LYRA_HTTP_PROXY` | `network.proxy` |
| `LYRA_WRITE_ENABLED` | `policy.write_enabled` |
| `LYRA_WEB_BIND` | `web.bind` |
| `LYRA_WEB_PORT` | `web.port` |
| `LYRA_ADMIN_USERNAME` | `web.admin_username` |
| `LYRA_ADMIN_PASSWORD` | `web.admin_password` |
| `LYRA_LRCLIB_AUTHORIZATION_REF` | `providers.lrclib.authorization_ref` |
| `LYRA_ACOUSTID_API_KEY` | `providers.acoustid.api_key` |
| `LYRA_AI_API_KEY` | `ai.api_key` |

环境变量只覆盖已有配置，不会自动创建新的受管根。多根部署请使用 `lyra.toml`。

## 完整示例

仓库中的 [`lyra.example.toml`](https://github.com/astralwaveorg/lyra/blob/main/lyra.example.toml) 是本地运行模板，[`deploy/lyra.container.toml.example`](https://github.com/astralwaveorg/lyra/blob/main/deploy/lyra.container.toml.example) 是容器模板。复制后按注释修改，不要直接改模板文件。
