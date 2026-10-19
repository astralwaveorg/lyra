# 更新记录

完整记录见仓库根目录的 [CHANGELOG.md](https://github.com/astralwaveorg/lyra/blob/main/CHANGELOG.md)。

## 1.1.0

- 设置页新增大模型配置：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages
- 支持 API 地址、模型 ID、Token、超时和连接测试
- Token 保存在 `data/ai-token`（0600），不进入数据库

## 1.0.0

首个公开版本。

- 歌词抓取、审查、内嵌与 `.lrc` 写入
- MusicBrainz 标签刮削、逐字段对照、冲突处理、自动扫描
- AcoustID 指纹兜底
- FLAC / MP3 / OGG / Opus 字节级写入
- 按标签整理、空目录清理、预览与撤销
- 重复音频分类、分类过滤、分类全选与一键删除
- 回收站、写入/改名/整理日志、批量撤销
- API Token、移动端适配、深浅色主题
- Docker 多架构镜像、GitHub Actions、文档站
