# 维护

## 数据目录

```text
data/
├── lyra.db            SQLite 主库
├── lyra.db-wal        WAL
├── lyra.db-shm        共享内存
├── backups/           写入前备份
├── trash/             回收站
├── staging/           写入暂存
└── initial-password.txt  首次启动的随机密码
```

`initial-password.txt` 只在首次启动、且没有配置密码时生成。修改密码后可以删除。

## 备份策略

至少备份：

- 配置文件；
- 数据库；
- 需要保留的备份和回收站。

备份前停止写入或执行 WAL checkpoint。推荐：

```bash
docker compose stop
tar -czf lyra-$(date +%F).tar.gz config data
docker compose start
```

恢复：

```bash
docker compose stop
tar -xzf lyra-2026-01-01.tar.gz
docker compose start
```

恢复后检查 `/api/v1/health` 和「设置 → 运行信息」。

## 数据库迁移

迁移在启动时自动执行，版本号记录在 `meta.schema_version`。迁移前会生成 `data/lyra.db.v<旧版本>.<时间>.bak`。

迁移失败时：

1. 不要反复重启。
2. 查看 `docker compose logs lyra` 的错误。
3. 保留数据库和 `.bak` 文件。
4. 回退镜像版本，或提交 Issue 并附上版本号和错误日志（不要附完整数据库）。

## 备份目录

`data/backups/` 保存人工确认写入的完整备份。默认不自动淘汰。

- `backup_auto_prune = false` 时，达到容量上限会停止新增写入并告警。
- 需要自动清理时，设置 `backup_auto_prune = true` 和 `backup_max_bytes`。
- 清理备份前确认不再需要撤销。

## 回收站

`data/trash/` 保存被删除的文件和目录，按批次组织。

- 还原通过界面或整理日志完成。
- 永久删除可以手动清理，也可以在回收站页面操作。
- 回收站不自动清理。

## 日志与审计

- 写入记录在数据库，可在「日志 → 写入」查看。
- 改名记录在 `renames` 表。
- 整理记录在 `organize_ops` 表。
- 监控事件在 `watch_log` 表，自动滚动保留最近记录。
- 容器 stdout 只保留最近的运行日志，不应当作为唯一审计来源。

## 磁盘空间

常见占用：

- `data/backups/`：完整备份；
- `data/trash/`：删除的文件；
- `data/staging/`：写入暂存，正常完成后会清理；
- `data/lyra.db-wal`：WAL，checkpoint 后会缩小。

检查：

```bash
du -sh data/*
docker compose exec lyra lyra status
```

## 性能

- 大曲库首次统计会读取所有音频元数据，需要时间。
- 标签扫描受 MusicBrainz 限流影响；公共 API 每秒最多一次请求。
- 需要更快扫描时，自建 MusicBrainz 镜像并降低请求间隔。
- 指纹计算会解码整首音频，只在缺 MBID 或歧义时使用。

## 安全维护

- 定期更新镜像和依赖。
- 不要把 `data/` 或 `config/` 提交到 Git。
- 不要把端口直接暴露到公网。
- 定期检查 API 令牌，撤销不再使用的。
- 备份文件包含数据库和配置，按敏感数据保管。
