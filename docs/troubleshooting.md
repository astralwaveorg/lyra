# 故障排查

## 先看这三处

```bash
docker compose ps
docker compose logs --tail=100 lyra
curl -s http://127.0.0.1:8080/api/v1/health
```

如果 Web 正常但监控显示无心跳，检查 `lyra-watch` 是否运行：

```bash
docker compose logs --tail=100 lyra-watch
```

只运行 `lyra` 不运行 `lyra-watch` 时，界面显示「监控无心跳」是正常的。

## 页面打不开

**容器没起来。** 看 `docker compose ps` 和日志。常见原因是 `.env` 里 `LIBRARY_HOST_PATH` 没设置，Compose 直接报错。

**端口被占用。** 修改 `LYRA_PORT`，或停止占用 8080 的进程。

**反向代理 502。** 确认代理目标是 `127.0.0.1:8080`，且容器确实监听在该端口。

**登录后立刻 401。** 直连 HTTP 时 Cookie 的 `Secure` 判断依赖 `X-Forwarded-Proto`。反向代理要传这个头；本地 HTTP 访问应直接使用 `http://127.0.0.1:8080`。

## 登录失败

- 用户名默认 `admin`。
- 首次密码在 `data/initial-password.txt`。
- 如果配置了 `LYRA_ADMIN_PASSWORD`，以配置为准。
- 密码至少 10 位。

重置密码：

```bash
docker compose exec lyra lyra admin-password --password '新的强密码'
```

## 扫描不到文件

- 确认 `[[roots]].path` 是容器内路径，通常是 `/music`。
- 确认挂载路径正确：`docker compose exec lyra ls /music`。
- 确认文件扩展名在支持范围内：`.flac`、`.mp3`、`.ogg`、`.opus`。
- 扫描默认跳过符号链接、其他挂载点和临时文件。
- 检查目录权限：容器用户必须能读取。

## 标签扫描全部未匹配

- 检查 MusicBrainz 是否可达：`docker compose exec lyra curl -I https://musicbrainz.org`。
- 公共 API 有限流，间隔不能低于 1 秒。
- 自建镜像要接入正确的 Docker 网络。
- 如果来源不可用，Lyra 会拒绝扫描，而不是把结果写成未匹配。

## 标签写入 422 或字段错误

- 查看响应里的 `detail`，现在会显示具体字段和原因。
- 常见原因是提案已过期，文件在扫描后被修改。
- 重新扫描后再写入。
- 冲突字段需要人工确认，不会被自动覆盖。

## 歌词写入失败

- 检查写入开关和根目录 `read_only`。
- 检查磁盘空间和文件权限。
- 查看事务记录和备份。
- 如果音频哈希校验失败，写入会回滚；原文件应保持不变。

## 整理预览为空

- 确认选择的目录下有音频。
- 确认排除目录没有把目标全部排除。
- 文件已经符合命名规则时，状态是「已正确」，不会出现在移动列表。
- 目标路径冲突时不会覆盖，需要先处理冲突文件。

## 整理执行失败

- 跨卷移动会被拒绝；曲库和数据目录应在同一卷，或先把文件放到目标卷。
- 目标已存在时不会覆盖。
- 计划超过 6 小时或已执行过，需要重新生成预览。
- 查看整理日志里的失败原因。

## 重复检测没有结果

- 至少需要两个文件音频哈希相同，或标签/指纹满足分类条件。
- 相似分类要求标题、艺人相同，整组时长跨度不超过 2 秒。
- 点「立即检查」重新扫描。
- 旧版本产生的混合分类会提示重新检测。

## 删除后找不到文件

文件在回收站：

```bash
ls data/trash/
```

在「回收站」或「日志 → 整理」里还原。不要手动移动回收站里的文件，除非确定不需要撤销记录。

## 数据库损坏或迁移失败

- 停止容器，保留 `data/lyra.db*` 和 `.bak`。
- 不要删除 WAL 文件后强行启动。
- 从备份恢复，或回退镜像版本。
- 提交 Issue 时附版本号、数据库结构版本和错误日志，不要附完整数据库。

## 日志太大

Compose 已限制日志大小。若使用外部日志驱动，检查其保留策略。数据库内的监控日志会自动滚动；审计记录不建议自动删除。

## 收集诊断信息

```bash
docker compose ps
docker compose logs --tail=200 lyra
docker compose logs --tail=200 lyra-watch
docker compose exec lyra lyra status
docker compose exec lyra lyra config check --json
curl -s http://127.0.0.1:8080/api/v1/health
```

提交 Issue 时请附上这些输出，并说明 Lyra 版本、部署方式和复现步骤。不要附 API key、密码、数据库或完整曲库路径。
