# GenshinLyreTranscriber

把本地视频或音频转换为原神风物之诗琴 21 键琴谱的 Windows 离线工具。

> 当前状态：项目骨架开发中，尚不可用于实际转谱。已实现能力以仓库代码、测试和
> 发布说明为准。

## 目标

- 从视频或音频中提取选定音轨，并在本地完成音频到 MIDI 的转录。
- 清理音符、分析局部节奏、按策略量化，并映射到 21 键范围。
- 输出精确事件 JSON、可读文本谱、旧播放器兼容文本谱、MIDI、试听 WAV 和报告。
- 提供 Rust CLI 与 TUI；Python 转录核心通过版本化 JSONL 协议运行。
- 最终提供不需要用户预装 Python 或 FFmpeg 的 Windows x64 离线便携包。

首版以独奏素材为优先目标，保留处理多音与和弦的能力，但不承诺完整混音歌曲
能够获得可直接演奏的理想结果。

## 当前可用功能

目前尚未提供完整的用户转谱命令或 TUI，但已经具备可验证的 ONNX 处理基础：

- `glt` Rust 二进制可以构建并输出版本号。
- `glt_core` Python 包可以安装并运行最小测试。
- Python worker 探针可以使用固定 Basic Pitch ONNX 模型把 WAV 转为 MIDI。
- 数据协议、完整媒体处理、节奏分析和三类谱导出仍待实现。

## 开发环境

- Windows 10/11 x64
- Rust 1.92.0
- Python 3.12 与 uv

运行当前骨架检查：

```powershell
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
uv run --directory python ruff check .
uv run --directory python ruff format --check .
uv run --directory python mypy .
uv run --directory python pytest
```

构建并查看版本：

```powershell
cargo build --locked
cargo run --locked -p glt
```

## 文档

用户使用、架构、格式与发布文档将在对应功能通过验收后加入 `docs/`。本 README
不会把计划能力描述为已经实现的功能。

## 许可证

项目代码采用 [MIT License](LICENSE)。FFmpeg、模型权重和其他第三方组件使用各自
许可证；正式便携包会单独提供组件和许可清单。
