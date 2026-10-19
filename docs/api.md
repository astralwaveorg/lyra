# API

Lyra 的 Web 界面就是一套 `/api/v1` JSON 接口的消费者。脚本和移动端可以直接使用同一套接口。

完整 OpenAPI 文档在运行中的服务上：

- Swagger UI：`/api/docs`
- OpenAPI JSON：`/api/openapi.json`

## 鉴权

两种方式：

- **会话 Cookie**：浏览器登录后由服务端设置 `lyra_session`，HttpOnly，默认 7 天。
- **Bearer Token**：在「设置 → API 令牌」签发，或使用 CLI。适合脚本和移动端。

```bash
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8080/api/v1/summary
```

Token 明文只返回一次，数据库只存 SHA-256。撤销后立即失效。

## 登录

```bash
curl -c cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}' \
  http://127.0.0.1:8080/api/v1/auth/login
```

后续请求带 Cookie：

```bash
curl -b cookies.txt http://127.0.0.1:8080/api/v1/summary
```

退出：

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8080/api/v1/auth/logout
```

修改密码：

```bash
curl -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","current_password":"...","new_password":"..."}' \
  http://127.0.0.1:8080/api/v1/auth/password
```

## 主要接口

### 状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查，无需鉴权 |
| GET | `/api/v1/summary` | 汇总、计数、受管根 |
| GET | `/api/v1/stats` | 曲库统计 |
| GET | `/api/v1/watch` | 监控心跳与最近事件 |

### 曲库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/tracks` | 曲目列表，支持搜索、状态、问题过滤、分页 |
| GET | `/api/v1/tracks/{id}` | 曲目详情 |
| GET | `/api/v1/library/dirs` | 目录树 |
| GET | `/api/v1/files` | 文件列表 |
| POST | `/api/v1/files/delete` | 移入回收站 |
| GET | `/api/v1/trash` | 回收站列表 |
| POST | `/api/v1/trash/restore` | 还原 |
| POST | `/api/v1/trash/empty` | 永久删除回收站内容 |

### 歌词

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/staged` | 暂存候选 |
| GET | `/api/v1/staged/{id}` | 候选详情 |
| POST | `/api/v1/staged/{id}/accept` | 接受候选 |
| POST | `/api/v1/staged/{id}/reject` | 拒绝候选 |
| POST | `/api/v1/staged/safe-batch/accept` | 批量接受安全候选 |
| POST | `/api/v1/tasks/fetch` | 启动抓取任务 |
| POST | `/api/v1/tasks/apply` | 启动写入任务 |

### 标签

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/tags/summary` | 标签扫描状态与计数 |
| GET | `/api/v1/tags/proposals` | 提案列表 |
| POST | `/api/v1/tasks/tags-scan` | 启动标签扫描 |
| POST | `/api/v1/tags/proposals/{id}/apply` | 写入提案字段 |
| POST | `/api/v1/tags/proposals/{id}/skip` | 跳过提案 |
| POST | `/api/v1/tags/proposals/{id}/link` | 按 MusicBrainz 链接指定 |
| POST | `/api/v1/tags/proposals/bulk-apply` | 批量写入安全字段 |
| GET | `/api/v1/tags/history` | 写入历史 |
| POST | `/api/v1/tags/writes/{id}/undo` | 撤销写入 |

### 整理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/organize/preview` | 生成移动预览 |
| POST | `/api/v1/organize/execute` | 执行预览 |
| POST | `/api/v1/organize/cleanup-preview` | 空目录预览 |
| POST | `/api/v1/organize/cleanup` | 清理空目录 |
| GET | `/api/v1/organize/ops` | 整理记录 |
| POST | `/api/v1/organize/undo` | 单条或批量撤销 |

### 重复音频

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/dedupe/groups` | 分组结果 |
| POST | `/api/v1/tasks/dedupe-scan` | 启动检测 |
| POST | `/api/v1/dedupe/delete` | 按分类或按组删除 |
| POST | `/api/v1/dedupe/groups/{id}/dismiss` | 忽略一组 |

按分类删除：

```bash
curl -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"kind":"exact"}' \
  http://127.0.0.1:8080/api/v1/dedupe/delete
```

按组选择：

```bash
curl -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"group_id":12,"path":"_duplicates/copy-1.flac"}]}' \
  http://127.0.0.1:8080/api/v1/dedupe/delete
```

服务端会校验分类、组成员、建议删除标记和参照项。任意一组不合法时整批拒绝。

## 错误格式

错误返回 JSON：

```json
{"detail":"具体原因"}
```

常见状态码：

| 状态码 | 含义 |
| --- | --- |
| 400 | 参数不正确 |
| 401 | 未登录或 Token 失效 |
| 403 | 无权限或写入关闭 |
| 404 | 资源不存在 |
| 409 | 冲突、任务运行中、分类已失效 |
| 422 | 请求体或字段校验失败 |

## 任务

抓取、扫描、整理等长任务返回任务 ID：

```json
{"id":"...","kind":"fetch","state":"running"}
```

轮询：

```bash
curl -b cookies.txt http://127.0.0.1:8080/api/v1/tasks/<id>
```

任务状态落库，重启后不会丢失；中断的任务会标记为 interrupted。

## 安全建议

- 不要把 Token 写进前端代码或公开仓库。
- 生产环境只通过 HTTPS 暴露。
- 给脚本使用单独的 Token，按用途命名，定期撤销。
- 不要用管理员 Cookie 做长期自动化。
