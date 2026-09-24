# 项目文档

公开文档只描述产品能力、使用方式、构建流程和数据契约，不包含维护者的本地任务状态、
临时证据或私有路径。

## 用户文档

- [命令行使用](USAGE.md)：TUI 操作、CLI 参数、筛选规则、输出文件和退出码。
- [Nightly 下载](DOWNLOADS.md)：从固定 `nightly` tag 的 Pre-release 获取 Windows 制品。
- [故障排查](TROUBLESHOOTING.md)：环境、模型、FFmpeg、试听和输出问题。
- [参考播放器兼容契约](REFERENCE_PLAYER_CONTRACT.md)：兼容谱的固定解析与调度基线。
- [第三方组件与许可](THIRD_PARTY.md)：模型、FFmpeg 和 Python 依赖的许可证说明。

## 开发者文档

- [开发与构建](DEVELOPMENT.md)：架构、处理管线、测试、Worker 打包与资源管理。
- [Schema 与版本策略](../schemas/README.md)：版本化 JSON 契约、错误代码和共享 fixtures。

## 推荐阅读顺序

普通用户先阅读根目录 [README](../README.md)，然后按需要进入命令行、制品下载或故障排查。
开发者从开发与构建开始，再阅读 Schema；参与兼容谱相关修改前必须完整阅读参考播放器契约。
