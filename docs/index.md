# Lyra 文档

Lyra 是一个自托管的音乐库维护工具：补齐歌词和标签、按元数据整理目录、检查重复音频，并把所有改动记进可撤销的记录。

![概览](assets/screenshots/overview.png)

## 从这里开始

<div class="grid cards" markdown>

-   **安装**

    用 Docker Compose 在五分钟内跑起来，或者从源码安装。

    [安装与升级](installation.md)

-   **配置**

    曲库路径、监听地址、MusicBrainz、AcoustID、自动扫描与写入策略。

    [配置参考](configuration.md)

-   **使用**

    概览、音乐、歌词审查、标签刮削、曲库整理、回收站和日志。

    [使用指南](usage.md)

-   **部署**

    反向代理、权限、备份、健康检查与 NAS 注意事项。

    [部署与运维](deployment.md)

</div>

## 按主题浏览

| 主题 | 内容 |
| --- | --- |
| [标签刮削](tags.md) | MusicBrainz 匹配、逐字段判断、冲突处理和自动写入 |
| [曲库整理](organize.md) | 命名规则、重复音频分类、预览、删除与撤销 |
| [架构](architecture.md) | 组件、数据流、数据库与安全边界 |
| [API](api.md) | 鉴权、主要接口和调用示例 |
| [维护](maintenance.md) | 备份、恢复、升级、回收站与日志清理 |
| [故障排查](troubleshooting.md) | 常见错误、日志位置和排查顺序 |
| [开发](development.md) | 本地环境、测试、代码地图和发布流程 |
| [贡献](contributing.md) | 提交 Issue、PR 和文档的约定 |
| [安全](security.md) | 漏洞报告与部署加固 |
| [赞助与社群](sponsor.md) | 赞赏码、交流群和致谢 |

## 当前版本

Lyra 2.2.19，数据库结构 v19。更新记录见 [更新记录](changelog.md)。
