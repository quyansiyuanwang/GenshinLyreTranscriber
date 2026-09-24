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
| worker onedir 大小 | 293.76 MiB |
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

## 拍点与局部速度

音频使用逐 onset 间隔估计候选拍点和局部 BPM，而不是假定整曲恒定速度。拍点
写入 `beat_grid`，局部速度写入 `tempo_map`；少于最小拍点数、BPM 越界或置信度
不足时不伪造 BPM，保留原始时序并发送 `TIMING_FALLBACK` 告警。MIDI 输入保留已有
tempo map。

## 量化

量化在局部拍点上比较直拍四分细分与三连音三分细分。`auto` 只有在候选误差明显
更优时才选择，模糊网格保留原时序；`preserve` 不移动 onset；`straight`/`triplet`
强制执行指定网格。显式 BPM 和手动拍点优先于自动分析，超过最大位移或低置信
的单个音符单独回退并记录原因。

## 21 键映射

默认键位顺序为 `ZXCVBNMASDFGHJQWERTYU`，音高显式对应 C3-B5 的 21 个自然音。
自动移调在 -12..12 半音内评估半音替换、八度折返、同刻键冲突和旋律间隔失真，
按稳定评分选择；手动移调跳过搜索。半音等距时默认向低音自然音取整，越界按八度
折返。同一时刻映射到同键的音符会确定性去重并计数。

人工音高核对可生成顺序听音文件：

```powershell
uv run --directory python python -m glt_core.tools.mapping_check artifacts/mapping-check
```

输出 WAV 每个音之间留有空隙，JSON manifest 同时记录按键顺序和 MIDI 音高。

已清理 MIDI 可在正式映射前比较全部移位候选：

```powershell
uv run --directory python python -m glt_core.tools.mapping_preview cleaned.mid
uv run --directory python python -m glt_core.tools.mapping_preview cleaned.mid --json
```

预览包含评分、移调、音符数、半音替换、八度折返、同刻冲突和唯一键数；
`auto` 选择项带 `selected` 标记。

## 精确导出与原子发布

`score.events.json` 由同一组 `MappedEvent` 生成，同刻按键在写出前确定性合并并按整数
微秒排序；写出和重新读取都执行冻结的 events Schema 与语义校验。`cleaned.mid` 和
`mapped.mid` 使用 1000 ticks/四分音符的导出分辨率，回归测试要求起音 round-trip
误差不超过 1ms。报告生成后执行 report Schema 和相对路径校验。

worker 的所有产物先写入输出目录同级的隐藏 staging。`result` 到达后，Rust 根据报告
校验每个文件的存在性、字节数和 SHA256。首次发布使用目录重命名；覆盖已有结果时先把
旧目录移到隐藏备份，再发布完整 staging，失败会尝试恢复旧目录。源输入哈希在回归测试
和真实 worker 端到端中检查，覆盖路径包含源输入时直接拒绝。

## 轻量试听合成

`--preview-wav` 使用 44.1kHz 单声道 PCM16 输出 `preview.wav`。音色由三个谐波分量、
3ms 起音、指数衰减和 50ms 尾部释放自行合成，不包含或模拟游戏采样。整数微秒通过
`(at_us * sample_rate + 500_000) // 1_000_000` 映射到最近采样帧，平局向上，保证起音
误差不超过一个采样。和弦共享时轴并统一峰值归一化到 0.95，避免 PCM16 削波。

空谱不创建静音文件，报告写入 `EMPTY_PREVIEW`。合成属于 Python worker，不初始化音频
设备。Rust `preview` 模块通过 rodio 提供播放、暂停、恢复、停止和 0..=1 音量控制；
设备或解码失败返回可查询的降级错误，停止会释放解码器和音频流资源。

## TUI 与作业复用

TUI 使用 Ratatui/Crossterm，支持输入/输出路径、目录浏览、参数页、真实阶段、未知进度和
取消。表单通过 `build_job_options` 与 CLI 共用同一参数校验；作业通过
`run_job_with_cancel` 在后台线程运行，事件循环以 50ms 轮询键事件和作业消息，不复制
worker 启动、发布或协议逻辑。

终端进入 alternate screen 后由 RAII guard 负责退出时恢复 raw mode、光标和主屏幕。
`TestBackend` 覆盖 80x24 与 120x40 渲染边界；取消会设置共享原子标志并等待 worker
进程树清理。完整结果列表和试听控件在后续结果闭环中接入。

## 文本谱导出

`score.readable.txt` 只用于人工阅读，首行明确声明它不是旧播放器精确执行格式。拍点
可靠时按拍点分组但不推断拍号；没有可靠拍点时显示绝对时间轴。文件显示来源片段起点、
总时长、实际移调、单音/和弦图例，并说明起音间隔和尾部静音限制。

`score.compat.txt` 针对已核验的参考播放器语义生成：`INTERVAL_RATING=0.01`、
`SPACE_INTERVAL_RATING=1.0`、`LINE_INTERVAL_RATING=0.0`、单逻辑行、首尾斜杠保护。
起音按 10ms 取整，5000us 平局向上；同一槽的不同按键合并为和弦，同键重触发无法保留时
增加 `compatibility_collisions`。空谱没有音符主体，尾部静音只写入注释和报告，不伪造
旧播放器可等待的结束时间。

固定参考播放器为
`https://github.com/quyansiyuanwang/GenshinImpactPianoPlayer.git` 的提交
`7aaddad21b71e91fd36b6ce7687dfb546076f602`。适配验证只读取该提交的解析器和
播放器代码，并在虚拟时钟/键盘上执行，不发送真实按键：

```powershell
uv run --project python python scripts/verify_reference_player_contract.py `
  --player-root PATH_TO_GIPIOPLAYER
```

验证脚本会检查远端、提交和干净工作树，拒绝解析告警，并逐槽比较期望与虚拟调度结果。
详见 [参考播放器契约](REFERENCE_PLAYER_CONTRACT.md)。

## 模型资源

模型从固定的 Basic Pitch 提交下载，构建前必须运行：

```powershell
uv run --project python python scripts/fetch_resources.py basic-pitch
```

脚本会校验文件大小与 SHA256，模型不会提交到 Git。PyInstaller 将 `nmp.onnx` 与
`schemas/*.schema.json` 放入 worker 资源目录，运行时不依赖开发仓库路径，也不携带
TensorFlow 或 CoreML 模型。

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
