# 开发与构建

本文档面向修改源码、运行测试、构建 worker 或维护数据契约的开发者。用户操作说明见
[命令行使用](USAGE.md)，下载方式见 [制品下载](DOWNLOADS.md)。

## 支持范围

- 开发目标：Windows 10/11 x64
- Rust：由 `rust-toolchain.toml` 固定
- Python：CPython 3.12，由 `python/uv.lock` 固定
- 转录引擎：Basic Pitch 0.4.0 的 ONNX 模型，CPU 推理
- Worker 打包：PyInstaller 6.22.3 onedir

Basic Pitch 的 Python 元数据在部分平台会尝试安装旧的 TensorFlow 后端。本项目通过 `uv`
override 阻止该依赖，并显式使用 ONNX Runtime。运行时不包含 TensorFlow、CoreML 或 TFLite。

## 仓库结构

```text
crates/glt/              Rust CLI、TUI、作业控制器、试听
python/src/glt_core/     Python worker 与处理核心
  domain/                统一音符时间轴与 MIDI 导入
  media/                 ffprobe/FFmpeg 探测与提取
  transcription/         Basic Pitch ONNX 适配与分段
  processing/            清理、筛选、时序、量化、编排、映射
  export/                JSON、MIDI、两类文本谱
  synthesis/             自合成试听 WAV
  resources/             运行时模型与 Schema 资源
schemas/                 跨语言版本化数据契约
tests/fixtures/          可再分发的小型契约夹具
scripts/                 下载资源、构建 worker、契约验证
docs/                    用户与开发者文档
```

## 运行时架构

```mermaid
flowchart LR
    CLI[CLI / TUI] --> JOB[Job controller]
    JOB -->|JSONL v2| WORKER[Python worker]
    WORKER --> MEDIA[FFmpeg / MIDI import]
    MEDIA --> MODEL[Basic Pitch ONNX]
    MODEL --> CANDIDATES[Candidate notes]
    CANDIDATES --> FILTER[Filter]
    FILTER --> CLEAN[Clean / arrange]
    CLEAN --> TIMING[Timing / quantize]
    TIMING --> MAP[21-key mapping]
    MAP --> EXPORT[JSON / MIDI / text / WAV]
    EXPORT --> REPORT[Verified result directory]
```

Rust 负责参数、进程生命周期、协议校验、目录发布和本地播放。Python 负责媒体与音乐处理。
文件通过路径传递，JSONL 只承载控制消息，不承载原始媒体或完整音符集合。

## 开发环境

```powershell
uv sync --project python --locked
uv run --project python python scripts/fetch_resources.py basic-pitch
cargo build --locked
```

常用运行方式：

```powershell
cargo run --locked -p glt -- --help
cargo run --locked -p glt -- transcribe input.flac --output output --preview-wav
cargo run --locked -p glt -- convert-midi score.mid --output output
```

开发构建位于 `target/debug/glt.exe`。需要接近发布环境的性能时使用：

```powershell
cargo build --release --locked
```

## 本地质量检查

```powershell
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked

uv lock --check --directory python
uv run --directory python ruff check .
uv run --directory python ruff format --check .
uv run --directory python mypy .
uv run --directory python pytest
```

CI 还会检出固定提交的参考播放器，并运行兼容谱解析与虚拟键盘调度验证。

## 媒体输入与 FFmpeg

音频/视频先由 ffprobe 解析音轨，再由 FFmpeg 用参数数组执行选择、裁剪和重采样。代码不通过
shell 拼接用户文件名，目标音频默认转为单声道 22.05kHz，供 Basic Pitch 使用。

开发环境可通过以下变量指定工具：

```powershell
$env:GLT_FFMPEG_DIR = "C:\path\to\ffmpeg\bin"
```

也可以分别设置 `GLT_FFMPEG` 和 `GLT_FFPROBE`。缺少媒体工具时，媒体任务应在提取前明确
失败；纯 MIDI 转换不依赖 FFmpeg。

## Basic Pitch 转录

正式 adapter 使用 15 秒分段和 1 秒重叠。每个分段独立运行 ONNX，接缝融合仅合并来自不同
分段的同音高重叠事件，避免把同一分段内的重触发误合并。纯静音输入是合法空结果。

模型加载包含 ONNX Runtime 初始化，冷启动明显慢于后续分段。当前随包模型为 `nmp.onnx`，
下载脚本会验证固定大小与 SHA256；模型不作为源码提交。

Windows 11、CPython 3.12.12、9.1 秒单声道短音频的参考测量：

| 指标 | 结果 |
|---|---|
| 冻结 worker 冷启动总耗时 | 4.26-9.79 秒 |
| 热进程模型加载 | 1.61 秒 |
| 单文件推理 | 1.96-2.14 秒 |
| 峰值工作集 | 191.73-192.99 MiB |
| worker onedir 大小 | 293.76 MiB |
| FFmpeg `bin` 目录大小 | 175.20 MiB |

不同 CPU、磁盘和杀毒软件会影响测量，结果不是跨机器性能保证。

## MIDI 导入

`convert_midi` 支持 format 0/1，拒绝 format 2 和 SMPTE division。tempo 变化使用有理数
累计后按微秒取整，避免逐段浮点漂移。同音重叠使用 FIFO note-off 配对，velocity 0 视为
note-off。

以下情况会修复并报告，而不是静默生成错误时间轴：

- 缺失 note-off
- 孤立 note-off
- 零长度音符
- channel 10 打击乐
- 同 tick 冲突

## 候选缓存与筛选

Worker v2 在清理前生成 `score.candidates.json`，保存候选 NoteSequence、confidence、
velocity、原始 MIDI pitch、tempo/beat grid 和报告上下文。

`FilterSpec v1` 支持 confidence、`duration_ms`、velocity 和原始 MIDI pitch 的闭区间。
每条规则内使用 AND，启用规则之间使用 OR；`confidence` 为 `null` 时，含 confidence 条件的
规则不匹配。新转录会把旧清理阈值转换为初始规则。

`refilter` 操作只读取候选缓存，重新执行筛选、结构清理、量化、编排、映射和导出，不启动
FFmpeg 或 Basic Pitch。新结果复制 `source.mid` 与候选缓存，因此可以继续生成下一版本。
没有候选缓存的旧结果必须重新转录。

自动检测使用确定性分位数：

- 时长：15% 分位，约束到安全范围
- confidence：30% 分位，约束到安全范围
- pitch：30% 分位，并限制上限

推荐规则为“时长达到下限”或“confidence 与音高同时达到下限”，仍是标准 FilterSpec。

## 清理与可演奏性编排

默认清理参数为 confidence `0.2`、最短时长 `50ms`、重触发间隔 `30ms`。只有精确重复或
起点接近且确实重叠的同音事件会合并，正常重触发保留。输出 `cleaned.mid` 和逐类损失计数。

可演奏性编排默认启用，将 `150ms` 内的起音视为同一位置，并按力度、时值、音程关系保留
最多两个互补声部。编排发生在量化之前，避免近同时误检形成不可演奏的厚和弦。

## 拍点、局部速度与量化

音频使用稳定的拍点候选构建 `beat_grid`，再由相邻拍间隔计算局部 BPM。拍点不足、BPM 越界
或置信不足时保留原始 onset 并发送 `TIMING_FALLBACK`，不伪造全曲恒速。

量化比较直拍的十六分细分和三连音细分。`auto` 只在候选明显更优时选择；`preserve` 不移动，
`straight`/`triplet` 使用指定网格。显式 BPM 优先于自动速度估计。

## 21 键映射

默认布局为 C3-B5 的 21 个自然音键。自动移调优先保持音高类别，再搜索损失较小的整体半音
偏移；越界音符按八度折返。升降音符按稳定规则选择目标自然音级，并分别统计半音替换、
八度折返和同刻去重。

映射配置稳定排序，相同输入必须得到相同结果。映射后事件在相同微秒合并为和弦。

## 数据导出

### 精确 JSON

`score.events.json` 使用整数微秒，按时间严格递增；同一时刻只保留一个和弦事件。它由
`events-v1.schema.json` 校验，是播放器交接的精确时间来源。

### MIDI

- `source.mid`：原始模型结果或 MIDI 来源副本
- `cleaned.mid`：清理与量化结果
- `mapped.mid`：21 键映射结果

没有可信 tempo map 时，导出使用固定 tempo 和足够分辨率，保证起音 round-trip 误差不超过
1ms。原始输入不会被覆盖。

### 可读谱

`score.readable.txt` 首行明确说明它不是旧播放器精确执行格式。拍点可靠时按拍点分组但不
推断拍号；没有可靠拍点时显示绝对时间轴。它只表达起音，不表达按住时长和连音。

### 兼容谱

兼容谱以固定 10ms 槽编码，具体解析行为和验证方式见
[参考播放器兼容契约](REFERENCE_PLAYER_CONTRACT.md)。同一槽的不同按键可合并为和弦；
同键重触发不能表达时必须报告 collision。尾部静音不能由旧格式完整表示。

## 合成试听

`preview.wav` 使用项目自行合成的三谐波短衰减音色，不包含游戏采样。整数微秒通过：

```text
(at_us * sample_rate + 500_000) // 1_000_000
```

映射到最近采样帧，平局向上。和弦共享时轴并统一峰值归一化，避免 PCM16 削波。空谱不生成
静音文件，而写入 `EMPTY_PREVIEW`。合成不初始化音频设备；Rust 播放后端失败不会影响转换。

## Worker 协议与结果发布

Worker 使用 JSONL v2。worker 启动后先发送 `ready`，Rust 随后发送 `start` 或 `cancel`，并
接收 `progress`、`warning`、`result`、`error`、`cancelled`。消息必须匹配当前 `job_id`，
且有行长和结构限制。

worker 只写输出目录同级的隐藏 staging。Rust 根据 `result` 检查每个声明产物的相对路径、
文件大小和 SHA256；全部通过后才整目录发布。失败不留下看似成功的结果。取消会等待 worker
退出，超时后结束其进程树。

## 资源与打包

获取固定模型：

```powershell
uv run --project python python scripts/fetch_resources.py basic-pitch
```

构建 onedir worker：

```powershell
./scripts/build_worker.ps1
```

产物位于 `artifacts/worker/glt-worker`。打包必须包含模型和 Schema，不得依赖开发仓库相对
路径。发布前仍需验证：

- 非源码目录启动
- 缺资源时的明确错误
- 真实模型 smoke
- 无系统 Python/FFmpeg 环境
- 断网运行
- FFmpeg、模型与所有第三方许可证和 NOTICE

当前宿主没有可用的管理员级网络隔离环境，因此只能在发布候选包上完成真正的断网门禁。

## 自动化制品

Nightly 和 Debug workflow 上传 Actions artifacts，不创建正式 Release。下载步骤见
[制品下载](DOWNLOADS.md)。正式版本发布必须获得明确的版本、提交和发布方式批准。
