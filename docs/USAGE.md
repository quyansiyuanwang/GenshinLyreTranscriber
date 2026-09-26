# 命令行使用

本文档覆盖 TUI、CLI、筛选、试听、输出和退出码。可执行文件名以下统一写作 `glt`；
源码开发环境中可直接使用 `target\debug\glt.exe`。

## 快速开始

```powershell
# 进入交互界面
glt tui

# 检查运行环境
glt doctor

# 创建并校验参数/映射配置
glt config init --output my-config.json
glt config validate my-config.json

# 转录音频或视频并生成试听
glt transcribe input.flac --output output --preview-wav

# 导入 MIDI
glt convert-midi score.mid --output output

# 从候选缓存重新筛选
glt filter output --output output-filter --auto

# 播放结果中的 preview.wav
glt preview output --volume 0.8
```

## 命令总览

| 命令 | 用途 |
|---|---|
| `glt` / `glt tui` | 打开终端交互界面 |
| `glt doctor` | 检查 worker、模型和运行资源 |
| `glt config` | `init`、`validate`、`show` 版本化参数与 21 键映射 |
| `glt transcribe INPUT` | 转录音频或视频 |
| `glt convert-midi INPUT` | 导入并处理 MIDI |
| `glt filter RESULT_DIR` | 从已有候选缓存生成筛选版本 |
| `glt preview RESULT_DIR` | 播放结果中的合成试听 |

所有命令都支持 `--help`。例如：

```powershell
glt transcribe --help
glt filter --help
```

## TUI 操作

无参数运行 `glt` 或执行 `glt tui` 会进入 Ratatui 界面。TUI 与 CLI 共用参数校验和作业
控制器，相同的输入与参数产生相同配置。

### 通用按键

| 按键 | 行为 |
|---|---|
| `Tab` / `Shift+Tab` | 在字段之间移动 |
| `Enter` | 确认当前输入或进入下一阶段 |
| `Space` | 循环选项或切换布尔值 |
| `F2` | 浏览当前路径 |
| `F5` | 开始任务；筛选页中表示应用规则 |
| `C` | 参数页中打开配置与 21 键映射视图 |
| `Esc` | 返回或退出 |
| `Ctrl+C` | 取消正在运行的作业，或退出 TUI |

### 输入与参数页

- 输入素材路径和输出目录后按 `Enter` 进入参数页。
- 路径支持终端粘贴和从资源管理器拖入。bracketed paste 会去除外层引号。
- 音频/视频参数包括音轨、片段、时序、清理、编排、移调和预览。
- MIDI 输入会忽略媒体专有的音轨与时间片段参数。

### 结果页

| 按键 | 行为 |
|---|---|
| `Space` | 播放或暂停试听 |
| `S` | 停止播放并释放资源 |
| `+` / `-` | 调整试听音量 |
| `Up` / `Down` | 滚动结果详情 |
| `F` | 打开分组筛选编辑器 |
| `A` | 自动检测并应用筛选规则 |
| `B` | 应用 balanced 预设 |
| `M` | 应用 melody 预设 |
| `R` | 返回参数页，保留输入并重新执行 |

### 配置与映射页

在参数页按 `C` 打开版本化配置视图。这里可以浏览配置 JSON、导入/导出，并用图形化键位网格编辑 21 键映射。

| 按键 | 行为 |
|---|---|
| `Tab` | 在配置路径和映射网格之间切换焦点 |
| `F6` | 浏览并选择配置 JSON |
| `F7` | 导入当前路径配置 |
| `F8` | 导出当前参数和映射 |
| `Arrow` | 在 21 键网格中移动选择 |
| `Space` | 选择键位或与另一键交换音高 |
| `[` / `]` | 所选键位 ±1 半音 |
| `-` / `+` | 所选键位 ±12 半音 |
| `R` | 恢复默认 C 调映射 |

### 筛选页

| 按键 | 行为 |
|---|---|
| `Up` / `Down` | 选择规则组 |
| `Tab` / `Shift+Tab` | 选择范围字段 |
| `Space` | 启用或禁用规则 |
| `Delete` / `Backspace` | 清空或删除当前字段字符 |
| `F5` | 应用规则并异步生成新版本 |
| `R` | 恢复来源结果中的初始规则 |
| `Esc` | 返回结果页 |

每次应用生成新的 `原目录-filter-NN` 目录，不覆盖来源结果。无 `preview.wav` 时会提示重新
生成。终端退出和异常路径由 guard 恢复 raw mode、光标和主屏幕。

## 转录音频或视频

```text
glt transcribe INPUT --output DIR [OPTIONS]
```

常用示例：

```powershell
# 默认自动时序、自动移调
glt transcribe song.flac --output out --preview-wav

# 指定音轨并只处理 30 到 90 秒
glt transcribe concert.mp4 `
  --output out `
  --audio-track 1 `
  --start-seconds 30 `
  --end-seconds 90

# 已知曲速，强制十六分直拍
glt transcribe song.flac --output out --timing straight --bpm 120

# 保留原始时序，关闭可演奏性编排
glt transcribe rehearsal.wav --output out --timing preserve --arrangement off
```

| 参数 | 说明 |
|---|---|
| `--output DIR` | 结果目录，必填 |
| `--audio-track N` | 音轨位置编号，从 0 开始 |
| `--start-seconds S` | 片段起点，单位秒 |
| `--end-seconds S` | 片段终点，单位秒 |
| `--timing MODE` | `auto`、`preserve`、`straight`、`triplet` |
| `--bpm NUMBER` | 显式 BPM，优先于自动速度分析 |
| `--transpose VALUE` | `auto` 或 `-48..48` 半音 |
| `--config FILE` | 载入版本化参数与 21 键映射；显式参数优先于配置 |
| `--preview-wav` | 生成自行合成的 `preview.wav` |
| `--no-preview-wav` | 即使配置启用预览也显式关闭 |
| `--overwrite` | 允许替换工具此前生成的同名输出 |
| `--json` | stdout 输出机器可读结果 |
| `--worker PATH` | 覆盖 worker 路径，仅用于开发或诊断 |

多音轨默认选择媒体声明的默认音轨；没有默认时选择第一条。首版不会自动混合多个音轨。

## 转换 MIDI

```text
glt convert-midi INPUT --output DIR [OPTIONS]
```

支持 MIDI format 0/1，拒绝 format 2 和 SMPTE division。MIDI 默认使用 `preserve`，保留
已有 tempo map，不重新估计速度。

```powershell
glt convert-midi score.mid --output out --transpose auto --preview-wav
```

`--timing`、`--transpose`、清理和编排参数与音频转换共用。`--bpm`、`--audio-track`、
`--start-seconds` 和 `--end-seconds` 不适用于 MIDI。

## 清理配置

| 档位 | 默认规则 | 用途 |
|---|---|---|
| `auto` | confidence `0.2`、时长 `50ms` | 默认；为后续编排保留候选 |
| `solo` | confidence `0.2`、时长 `50ms` | 独奏素材 |
| `mix` | confidence `0.4`、时长 `100ms` | 较嘈杂素材 |
| `strict` | confidence `0.5`、时长 `150ms` | 更激进地去除弱音和短音 |

单项参数可以覆盖档位：

```powershell
--min-confidence 0..1
--min-duration-ms N
--retrigger-gap-ms N
```

实际值写入 `CLEANING_CONFIG` 告警。提高阈值可能丢失弱音或和声，建议保留原结果后再重筛。

## 可演奏性编排

默认 `--arrangement balanced` 会将 `150ms` 内的近同时起音视为同一演奏位置，并按力度、
时值和音程保留最多两个互补声部：

```powershell
--arrangement balanced|off
--onset-window-ms 150
--max-voices 2
```

关闭编排会保留更多细节，但更容易产生难以演奏的厚和弦。删除统计写入 `ARRANGEMENT_LOSS`。

## 时序与量化

| 模式 | 行为 |
|---|---|
| `auto` | 比较直拍与三连音候选；证据不足时保留原始 onset |
| `preserve` | 不移动起音 |
| `straight` | 强制使用十六分直拍网格 |
| `triplet` | 强制使用三连音网格 |

音频 `auto` 会建立局部拍点；低置信时写入 `TIMING_FALLBACK`，不会伪造全曲固定 BPM。
`--bpm` 是显式覆盖，优先级最高。

## 移调与 21 键映射

默认琴为 C 调，映射范围为 C3-B5。`--transpose auto` 优先保留音高类别，再以最少损失选择
整体移调；也可以指定 `-48..48` 半音。越界音按八度折返，升降音符按稳定规则选择自然音级。

报告会分别统计：

- 半音替换
- 八度折返
- 同刻映射键去重
- 被丢弃或合并的音符
- 自动时序回退

映射前的候选预览可通过开发工具执行：

```powershell
uv run --directory python python -m glt_core.tools.mapping_preview output\cleaned.mid
```

### 可移植配置

`glt-config-v1` 同时包含清理、编排、时序、移调参数和 21 键布局。桌面端、CLI 和 TUI
使用同一结构；命令行动态参数优先于配置，配置优先于内置默认值。

```powershell
glt config init --output song-config.json --name "远航星 C 调"
glt config validate song-config.json
glt config show song-config.json
glt transcribe input.flac --output out --config song-config.json --transpose 12
```

未知版本、重复键位、重复音高或超出 C3-B5 自然音的配置会被拒绝，不会覆盖已有可用文件。

## 筛选结果

筛选不会重新运行 FFmpeg 或 Basic Pitch，只读取结果目录中的 `score.candidates.json`。

### 自动与预设

```powershell
glt filter output --output output-auto --auto
glt filter output --output output-balanced --preset balanced
glt filter output --output output-melody --preset melody
glt filter output --output output-clean-only --preset off
```

- `auto`：根据候选分布的时长、confidence 和音高分位生成规则。
- `balanced`：保留中长音，并减弱短促鼓型误检。
- `melody`：偏向较长、较高置信音符。
- `off`：不按属性筛选，仅执行结构清理。

自动检测的门槛和命中统计会写入 `FILTER_AUTO` 告警及报告。

### 自定义规则

```json
{
  "format_version": 1,
  "rules": [
    {
      "enabled": true,
      "confidence": {"min": 0.25, "max": 1.0},
      "duration_ms": {"min": 120, "max": 60000},
      "velocity": {"min": 1, "max": 110},
      "pitch": {"min": 36, "max": 96}
    },
    {
      "enabled": true,
      "duration_ms": {"min": 500, "max": 60000}
    }
  ]
}
```

```powershell
glt filter output --output output-custom --filter-file filter.json
```

规则语义：

- 每条规则内所有填写范围使用 AND。
- 不同启用规则之间使用 OR。
- 范围包含最小值和最大值。
- 未填写的属性不参与判断。
- `confidence` 缺失时，含 confidence 条件的规则不匹配。
- `pitch` 使用原始 MIDI 编号或 `C#4` 音名，不受移调影响。
- 每条规则最多四个可选范围；CLI Schema 支持最多八条规则，TUI 提供四组编辑槽。

旧结果没有候选缓存时，`glt filter` 会明确失败并要求重新转录。

## 试听

```powershell
glt preview output --volume 0.8
```

预览文件是自行合成的 PCM16 WAV，不包含游戏采样。`--preview-wav` 不改变 JSON、MIDI 或
文本谱；空谱不会生成静音文件，而会写入 `EMPTY_PREVIEW`。没有音频设备只影响播放，不影响
转换与 WAV 导出。

## 输出目录

| 文件 | 说明 |
|---|---|
| `source.mid` | 模型原始转录；MIDI 输入时为逐字节来源副本 |
| `score.candidates.json` | 清理前候选音符、confidence、velocity 和 timing 元数据 |
| `cleaned.mid` | 清理、量化后的中间 MIDI |
| `mapped.mid` | 21 键映射后的 MIDI |
| `score.events.json` | 精确映射起音，整数微秒 |
| `score.readable.txt` | 面向人工阅读的谱面 |
| `score.compat.txt` | 固定参考播放器的 10ms 近似谱 |
| `preview.wav` | 启用预览且存在起音时生成 |
| `report.json` | 参数、统计、告警、产物大小与 SHA256 |

worker 先写入输出目录同级的隐藏 staging。Rust 校验所有声明产物的大小和 SHA256 后整体发布。
已有输出默认失败；只有显式 `--overwrite` 才会替换，不会混合新旧文件。输入文件不会被修改。

## 日志、JSON 与退出码

- 进度和告警写入 stderr。
- `--json` 模式下 stdout 只输出一个 JSON 对象。
- 普通模式 stdout 输出结果摘要。

| 退出码 | 含义 |
|---:|---|
| `0` | 成功，包括合法空事件结果 |
| `2` | 参数或输入错误 |
| `3` | 环境、worker 或功能不可用 |
| `4` | 处理失败或 worker 协议失败 |
| `5` | 输出失败 |
| `130` | 用户取消 |

`Ctrl+C` 会发送取消请求，等待最多 5 秒；worker 未退出时会关闭输入并清理其进程树。

## Worker 与工具发现

未指定 `--worker` 时，Rust 按以下顺序查找：

1. `GLT_WORKER_PATH`
2. 主程序相邻的 `glt-worker/glt-worker.exe`
3. 主程序相邻的 `glt-worker.exe`

FFmpeg 与 ffprobe 按以下顺序查找：

1. 显式路径
2. `GLT_FFMPEG_DIR`
3. `GLT_FFMPEG` 与 `GLT_FFPROBE`
4. 冻结应用内的资源目录
5. 开发环境的系统 `PATH`

源码开发可直接使用 Python 入口：

```powershell
glt transcribe input.mp4 --output output --worker python\src\glt_core\worker.py
```

下载 Nightly/Debug 制品见 [制品下载](DOWNLOADS.md)，常见问题见
[故障排查](TROUBLESHOOTING.md)。
