# 更新记录

完整记录见仓库根目录的 [CHANGELOG.md](https://github.com/astralwaveorg/lyra/blob/main/CHANGELOG.md)。

## 1.1.1

- 重复检测每一份都能勾选：「建议保留」的可以手动改选删除，「建议删除」的默认勾上
- 勾上建议保留项时同组其余自动取消勾选，每组始终至少保留一份
- 新增「批量删除所选」，一次处理所有勾选项
- 「忽略这组」改为持久生效，扫描后不再自己冒出来，并可随时恢复

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
