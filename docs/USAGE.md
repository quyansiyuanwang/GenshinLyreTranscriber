# 命令行使用

当前 Rust 前端已提供命令解析、worker 启动、JSONL 状态处理、取消和退出码。正式
worker 已支持从本地音频/视频转录或导入 MIDI，并输出 `source.mid`、`cleaned.mid`、
`mapped.mid`、`score.events.json`、`score.readable.txt`、`score.compat.txt` 与
`report.json`。

```powershell
glt --help
glt tui
glt doctor [--worker PATH] [--json]
glt transcribe INPUT --output DIR [OPTIONS]
glt convert-midi INPUT --output DIR [OPTIONS]
glt preview RESULT_DIR [--volume 0..1]
```

## TUI

无参数或执行 `glt tui` 会进入 Ratatui 界面。先输入素材路径和输出目录，按 Enter 进入
参数页；`Tab` 移动字段，`Space` 循环选择或者切换布尔值，`F5` 启动，`Esc` 退出，
`Ctrl+C` 取消正在运行的作业。`F2` 打开当前目录浏览；选择文件后回到路径输入。也可以把
文件从资源管理器拖入终端，或使用终端粘贴；bracketed paste 会去除外层引号并自动填入当前
路径字段。

运行页只显示 worker 实际发送的阶段和进度；没有可信百分比时明确显示未知。完成后结果页
显示计数、告警和产物，`Space` 播放/暂停、`S` 停止、`+/-` 调整音量，`R` 可带当前参数
重试；没有 `preview.wav` 时明确提示重新生成。TUI 与 CLI 共用参数构造和作业控制器，
终端退出或异常路径通过 guard 恢复 raw mode、光标和主屏幕。

## 常用参数

- `--timing auto|preserve|straight|triplet`
- `--bpm NUMBER`，仅音频转录使用
- `--transpose auto|INTEGER`
- `--audio-track N`
- `--start-seconds SECONDS`、`--end-seconds SECONDS`
- `--preview-wav`：按映射起音生成自合成轻量试听 WAV
- `--min-confidence 0..1`：最低 Basic Pitch 音符置信度，默认 `0.2`
- `--min-duration-ms N`：最短音符时长，默认 `50`
- `--retrigger-gap-ms N`：重触发/重叠合并间隔，默认 `30`
- `--overwrite`
- `--json`
- `--worker PATH`，仅开发或高级诊断使用

`glt preview RESULT_DIR` 读取 `report.json` 中的 `preview_wav` artifact 并播放；音量范围为
`0..=1`。没有预览文件时提示重新执行并加 `--preview-wav`。播放设备不可用只影响预览命令，
不会影响已有结果。

`--preview-wav` 只影响试听产物和试听控制，不改变 JSON、MIDI 或文本谱；空谱不会生成
静音文件，而是在报告中给出 `EMPTY_PREVIEW`。合成不依赖音频输出设备。

清理阈值可由用户显式调整，并会写入 `CLEANING_CONFIG` 警告记录。完整混音建议先尝试
`--min-confidence 0.4 --min-duration-ms 100`，再用 `--min-confidence 0.5 --min-duration-ms 150`
评估更严格的结果；提高阈值可能牺牲独奏中的弱音和装饰音。

`--timing auto` 比较直拍与三连音候选；证据不足时保留原始起音。`preserve`
完全不改时间，`straight`/`triplet` 强制使用对应网格。`--bpm` 是显式速度
覆盖，优先于自动分析。

没有传入 `--worker` 时，程序读取 `GLT_WORKER_PATH`，然后查找与主程序相邻的
`glt-worker` 目录。程序不会通过 shell 拼接输入路径。

源码开发时可直接选择 Python 入口：

```powershell
glt transcribe input.mp4 --output output --worker python/src/glt_core/worker.py
```

生成的 `cleaned.mid` 可用以下命令预览不同移调档位：

```powershell
uv run --directory python python -m glt_core.tools.mapping_preview output/cleaned.mid
```

## 输出与退出码

成功结果的核心文件如下：

- `source.mid`：音频转录的原始结果；MIDI 输入时为逐字节来源副本。
- `cleaned.mid`：清理和所选用时序策略后的 MIDI。
- `mapped.mid`：映射到 21 键后的 MIDI。
- `score.events.json`：按整数微秒记录映射起音和按键，同一时刻只保留一个和弦事件。
- `score.readable.txt`：带时间戳和图例的人工阅读谱；文件首行明确它不是旧播放器精确执行格式。
- `score.compat.txt`：按参考播放器 10ms 网格编码的兼容谱；网格碰撞和省略的尾部静音会写入报告。
- `preview.wav`：仅在 `--preview-wav` 且存在可演奏起音时生成；使用自行合成的短衰减音色，不包含游戏采样。
- `report.json`：版本、输入哈希、参数、损失统计以及每个产物的 SHA256 和字节数。

worker 只写输出目录同级的隐藏 staging。Rust 会验证已声明产物存在且大小、SHA256
完全匹配；首次成功时整目录发布。已有输出默认失败并返回 5，只有显式 `--overwrite`
才会整目录替换，不会把新旧产物混合。输入文件不会被修改；输入位于待覆盖输出目录
内时操作会被拒绝。

进度和警告写入 stderr；`--json` 模式下 stdout 只输出一个 JSON 对象。普通模式
stdout 输出结果目录与报告摘要。

| 退出码 | 含义 |
|---:|---|
| 0 | 成功，包括合法空事件结果 |
| 2 | 参数或输入错误 |
| 3 | 环境、worker 或功能尚不可用 |
| 4 | 处理失败或 worker 协议失败 |
| 5 | 输出失败 |
| 130 | 用户取消 |

Ctrl+C 会发送 `cancel`，等待最多 5 秒；worker 未退出时关闭输入并结束其进程树。
