# GenshinLyreTranscriber

把本地视频、音频或 MIDI 转换为原神风物之诗琴 21 键琴谱的 Windows 离线工具。项目以
独奏素材为优先目标，同时保留多音与和弦能力。

> 当前状态：CLI、音频/MIDI 处理链、三类琴谱导出、TUI 结果与试听闭环均已可运行。
> 项目尚未正式发布；离线便携包、真实设备试听和音乐质量人工确认仍待完成。

## 主要能力

- 本地音频/视频转录：FFmpeg 提取音轨，Basic Pitch ONNX CPU 推理。
- 精确处理链：音符清理、局部节拍分析、可配置量化、自动移调与 21 键映射。
- 三种谱面输出：精确事件 JSON、人工阅读谱、旧播放器兼容谱。
- MIDI 直转：支持 format 0/1，保留已有 tempo map。
- 结果重筛：从候选缓存按 confidence、时长、力度和原始 MIDI 音高重新筛选，不重复运行模型。
- 可试听预览：自行合成轻量 WAV，并通过 TUI 或 CLI 播放，不包含游戏采样。
- 完整离线目标：最终发布包不要求用户预装 Python、FFmpeg 或下载模型。

## 快速开始

### 环境

- Windows 10/11 x64
- Rust 1.92.0
- Python 3.12、[uv](https://docs.astral.sh/uv/)

### 构建

```powershell
uv sync --project python --locked
uv run --project python python scripts/fetch_resources.py basic-pitch
cargo build --locked
```

开发构建位于 `target/debug/glt.exe`，发布构建位于 `target/release/glt.exe`。使用 cargo
运行时，命令参数放在 `--` 之后，例如：

```powershell
cargo run --locked -p glt -- --help
```

### 使用 TUI

无参数启动，或显式运行 `tui`：

```powershell
target\debug\glt.exe
# 或
target\debug\glt.exe tui
```

TUI 支持路径粘贴、文件拖入、目录浏览、参数调整、任务取消、结果查看、试听和失败后重试。
完整按键说明见 [命令行使用](docs/USAGE.md#tui-操作)。

### 使用 CLI

转录音频或视频并生成试听 WAV：

```powershell
target\debug\glt.exe transcribe "input.flac" --output "output" --preview-wav
```

导入 MIDI：

```powershell
target\debug\glt.exe convert-midi "score.mid" --output "output"
```

从已有结果重新筛选：

```powershell
target\debug\glt.exe filter "output" --output "output-filter" --auto
```

播放已完成结果的试听文件：

```powershell
target\debug\glt.exe preview "output" --volume 0.8
```

## 输出文件

每次成功转换都会发布一个完整结果目录：

| 文件 | 说明 |
|---|---|
| `source.mid` | 模型原始转录结果；MIDI 输入时为来源副本 |
| `score.candidates.json` | 筛选前候选音符及 timing 元数据 |
| `cleaned.mid` | 清理和量化后的中间结果 |
| `mapped.mid` | 映射到 21 键后的 MIDI |
| `score.events.json` | 精确映射起音事件，整数微秒 |
| `score.readable.txt` | 面向人工阅读的谱面 |
| `score.compat.txt` | 面向兼容播放器的 10ms 近似谱 |
| `preview.wav` | 启用 `--preview-wav` 时生成的轻量试听 |
| `report.json` | 参数、统计、告警以及产物大小与 SHA256 |

Rust 会先验证 worker 产物，再整体发布结果目录。已有输出默认不会被覆盖，必须显式使用
`--overwrite`。

## 筛选与调参

结果页按 `F` 打开筛选编辑器，按 `A` 使用自动检测，或用 `B`/`M` 应用 balanced/melody
预设。CLI 也支持标准筛选文件：

```powershell
target\debug\glt.exe filter "output" --output "output-filter" --filter-file "filter.json"
```

筛选规则组内使用 AND、组间使用 OR；范围均包含边界。自动检测和预设只读取
`score.candidates.json`，不会重新执行 FFmpeg 或 Basic Pitch。详细字段和示例见
[命令行使用](docs/USAGE.md#筛选结果)。

## 文档

- [文档索引](docs/README.md)
- [命令行使用](docs/USAGE.md)
- [开发与构建](docs/DEVELOPMENT.md)
- [故障排查](docs/TROUBLESHOOTING.md)
- [参考播放器兼容契约](docs/REFERENCE_PLAYER_CONTRACT.md)
- [Schema 与版本策略](schemas/README.md)
- [第三方组件与许可](docs/THIRD_PARTY.md)

## 开发检查

```powershell
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
uv run --directory python ruff check .
uv run --directory python ruff format --check .
uv run --directory python mypy .
uv run --directory python pytest
```

## 当前限制

- Basic Pitch 对复杂混音、鼓声和重叠人声可能产生误检；当前版本通过清理、编排和结果重筛改善，
  不承诺完整混音歌曲的理想效果。
- `score.compat.txt` 使用 10ms 近似网格，无法表达同键重触发和完整尾部静音；
  `score.events.json` 才是精确时间依据。
- 当前正式目标为 Windows x64；跨平台与 GUI 尚未实现。
- 项目的正式离线包和发布验收尚未完成。

## 许可证

项目代码采用 [MIT License](LICENSE)。FFmpeg、Basic Pitch 模型和其他第三方组件适用各自
许可证，详见 [第三方组件与许可](docs/THIRD_PARTY.md)。
