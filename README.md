# GenshinLyreTranscriber

把本地视频或音频转换为原神风物之诗琴 21 键琴谱的 Windows 离线工具。

> 当前状态：CLI、核心处理与导出链、TUI 输入/参数/进度和试听基础已可运行；完整结果/
> 试听闭环和质量验收尚未完成，暂未达到可发布标准。

## 目标

- 从视频或音频中提取选定音轨，并在本地完成音频到 MIDI 的转录。
- 清理音符、分析局部节奏、按策略量化，并映射到 21 键范围。
- 输出精确事件 JSON、可读文本谱、旧播放器兼容文本谱、MIDI、试听 WAV 和报告。
- 提供 Rust CLI 与 TUI；Python 转录核心通过版本化 JSONL 协议运行。
- 最终提供不需要用户预装 Python 或 FFmpeg 的 Windows x64 离线便携包。

首版以独奏素材为优先目标，保留处理多音与和弦的能力，但不承诺完整混音歌曲
能够获得可直接演奏的理想结果。

## 当前可用功能

当前已提供 `transcribe`、`convert-midi` 与 `tui` CLI；结果/试听完整闭环尚未完成：

- `glt` Rust 二进制可以构建并输出版本号。
- `glt doctor` 可以启动协议 worker 并显示 worker、应用和模型版本。
- CLI 已定义 `transcribe`、`convert-midi`、`preview` 与 `tui` 命令及退出码。
- `glt_core` Python 包可以安装并运行最小测试。
- Python 媒体模块可以通过 ffprobe 选择音轨，并用参数数组调用 FFmpeg 提取模型音频。
- Python JSONL worker 可以完成媒体探测、音频提取、Basic Pitch ONNX 分段转录与 MIDI 导入。
- 清理阶段可以过滤低置信/短音、稳定去重与重叠合并，并输出 `cleaned.mid` 和损失计数；置信度、最短时长和重触发间隔均可配置。
- 自动拍点与局部速度分析支持低置信回退，保留原始 onset 时序。
- 支持 auto/preserve/straight/triplet 可控量化，显式 BPM 具有更高优先级。
- 支持全曲自动/手动移调、C3-B5 自然音映射、半音替换、八度折返和同刻冲突统计。
- 提供 -12..12 半音移调候选预览，可在正式映射前查看损失和冲突。
- 结果目录包含 `source.mid`、`cleaned.mid`、`mapped.mid`、`score.events.json`、`score.readable.txt`、`score.compat.txt` 和 `report.json`；按需生成 `preview.wav`。
- 精确事件 JSON 使用整数微秒，冻结 Schema 校验事件顺序与时长；MIDI 起音 round-trip 误差不超过 1ms。
- `score.readable.txt` 面向人工阅读并明确不是旧播放器执行格式；`score.compat.txt` 使用参考播放器的 10ms 多行分段结构，并按 `.qymusic` 常见的每行 4 段组织；网格碰撞与尾部静音仍会报告。
- `--preview-wav` 按映射起音生成自合成轻量 WAV，不包含游戏采样；空谱不会伪造可听文件。
- Ratatui/Crossterm TUI 提供输入/输出路径、文件浏览、参数确认、真实阶段/未知进度和取消；结果页显示损失/产物并控制试听，失败后可返回参数重试；支持拖入或粘贴文件路径。
- Rust rodio 播放控制已接入 TUI 完成页和 `glt preview RESULT_DIR --volume 0..1`，设备不可用时保留可诊断降级状态。
- worker 先写隐藏 staging，Rust 校验产物大小和 SHA256 后再发布；已有结果必须显式 `--overwrite`。

CLI 命令行为、worker 发现规则和退出码见 [docs/USAGE.md](docs/USAGE.md)。

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

命令、参数、输出和退出码见 [docs/USAGE.md](docs/USAGE.md)，构建与协议约束见
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 和 [schemas/README.md](schemas/README.md)。
本 README 不会把计划能力描述为已经实现的功能。

## 许可证

项目代码采用 [MIT License](LICENSE)。FFmpeg、模型权重和其他第三方组件使用各自
许可证；正式便携包会单独提供组件和许可清单。
