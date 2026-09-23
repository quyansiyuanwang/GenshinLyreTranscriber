# 开发与构建环境

## 支持目标

首版开发目标为 Windows 10/11 x64。Python worker 使用 CPython 3.12，依赖由
`python/uv.lock` 锁定；Rust 工具链由 `rust-toolchain.toml` 固定。

## 已验证的转录组合

| 组件 | 版本 | 用途 |
|---|---|---|
| CPython | 3.12.12 | Python worker 与打包构建 |
| basic-pitch | 0.4.0 | 官方模型与后处理 |
| ONNX Runtime | 1.30.0 | CPU-only 推理 |
| NumPy | 2.5.3 | 数值处理 |
| PyInstaller | 6.22.3 | onedir worker 打包 |

Basic Pitch 的元数据在 Windows 和 Python 3.11 以上会尝试安装旧版 TensorFlow。
本项目通过 `uv` override 阻止该可选后端进入锁定环境，并显式安装 ONNX Runtime。
运行时不包含 TensorFlow、CoreML 或 TFLite。

## 当前测量基线

在 Windows 11 `10.0.26200`、Python 3.12.12 上，使用随包的
`nmp.onnx` 和 9.1 秒的单声道短音频测量：

| 指标 | 结果 |
|---|---|
| 冻结 worker 冷启动总耗时 | 4.26-9.79 秒 |
| 热进程模型加载 | 1.61 秒 |
| 单文件推理 | 1.96-2.14 秒 |
| 峰值工作集 | 191.73-192.99 MiB |
| worker onedir 大小 | 292.77 MiB |
| FFmpeg `bin` 目录大小 | 175.20 MiB |

模型加载时间包含 ONNX Runtime 初始化；不同机器的 CPU 和磁盘速度会影响结果。
当前 worker 仍包含 NumPy、SciPy、Numba 和 LLVM 运行库，后续必须在发布前继续
评估双运行时包体积。

## 转录分段

正式 adapter 使用 15 秒分段、1 秒重叠。每个分段单独运行 ONNX 后只保留音符事件，
接缝融合仅合并来自不同分段的同音高重叠事件，避免把同一分段内的重触发合并。
当前输出为 `source.mid`；纯静音是成功空结果。

## MIDI 导入

`convert_midi` 支持 MIDI format 0/1，拒绝 format 2 和 SMPTE division。Tempo 改变
使用有理数累计后按微秒取整，避免逐段浮点漂移；同音重叠使用 FIFO note-off 配对，
velocity 0 视为 note-off。缺失 note-off、孤立 note-off、零长度修复和 channel 10
打击乐过滤都会写入 warning/report。

## 音符清理

清理默认参数为最低置信度 0.2、最短音长 50ms、重触发间隔 30ms。只有精确重复或
起点非常接近且确实重叠的同音事件会合并；正常重触发保留。输出包含 `cleaned.mid`
和逐类计数；清理后仍执行 NoteSequence 范围与排序校验。

## 模型资源

模型从固定的 Basic Pitch 提交下载，构建前必须运行：

```powershell
uv run --project python python scripts/fetch_resources.py basic-pitch
```

脚本会校验文件大小与 SHA256，模型不会提交到 Git。PyInstaller 只将
`nmp.onnx` 放入 worker 资源目录，不携带 TensorFlow 或 CoreML 模型。

## 本地检查

```powershell
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
uv run --directory python ruff check .
uv run --directory python ruff format --check .
uv run --directory python mypy .
uv run --directory python pytest
```

## Worker 打包

```powershell
uv run --project python python scripts/fetch_resources.py basic-pitch
./scripts/build_worker.ps1
```

构建产物位于 `artifacts/worker/glt-worker`，入口使用正式 JSONL worker 协议。发布前还需运行真实模型 smoke、
资源缺失错误、非源码目录启动、无系统 Python/FFmpeg 和离线验收。

当前宿主未提供可用的管理员级网络隔离环境，因此只完成了无效代理、精简
`PATH` 和进程连接监测。真正断网的干净 Windows 验收仍必须在发布候选包上执行，
当前结果不能替代该门禁。

开发环境可按以下方式让 Rust CLI 使用 Python 入口：

```powershell
$env:GLT_FFMPEG_DIR = "path/to/ffmpeg/bin"
glt transcribe input.mp4 --output output --worker python/src/glt_core/worker.py --json
```

## 媒体工具发现

媒体模块按以下顺序定位 FFmpeg 与 ffprobe：

1. 显式传入的工具路径。
2. `GLT_FFMPEG_DIR` 指向的同一目录。
3. 同时设置的 `GLT_FFMPEG` 与 `GLT_FFPROBE`。
4. 冻结应用内的相对资源目录。
5. 开发环境下系统 `PATH`。

`--audio-track` 使用音频流位置编号，从 0 开始；实际 ffprobe stream index 由模块
记录并传给 FFmpeg。裁剪时间使用整数微秒，输出始终先写 `.partial` 文件，成功后
原子替换。媒体测试：

```powershell
uv run --directory python pytest tests/test_media.py
```
