# 故障排查

先运行环境检查：

```powershell
target\debug\glt.exe doctor --json
```

`doctor` 会检查 worker、模型和资源版本。处理失败时优先查看终端 stderr 与结果目录中的
`report.json`；两种谱面不同、筛选结果异常或试听不可用时，通常都能在报告告警中找到原因。

## 找不到 worker

表现：`worker was not found` 或退出码 `3`。

源码开发环境可显式指定 Python worker：

```powershell
target\debug\glt.exe transcribe input.flac `
  --output output `
  --worker python\src\glt_core\worker.py
```

也可以设置 `GLT_WORKER_PATH`。如果使用发布包，请确认 `glt-worker/glt-worker.exe` 与
`glt.exe` 的相对位置没有被移动。不要从单个 `glt.exe` 中推断 worker 已随包安装。

## 找不到 FFmpeg 或 ffprobe

音频/视频转录需要 FFmpeg 与 ffprobe；纯 MIDI 转换不需要。开发环境可按以下方式指定：

```powershell
$env:GLT_FFMPEG_DIR = "C:\path\to\ffmpeg\bin"
```

也可以分别设置 `GLT_FFMPEG` 和 `GLT_FFPROBE`。请使用来源、版本和许可证明确的构建；
项目不会自动下载未经验证的二进制。

## 找不到模型

源码环境先执行：

```powershell
uv run --project python python scripts/fetch_resources.py basic-pitch
```

模型文件不会提交到 Git。发布包应从自身资源目录定位模型，不应依赖开发仓库路径。

## 输出目录已存在

工具默认拒绝覆盖已有输出，以避免混入旧文件。确认无需保留旧结果后添加：

```powershell
--overwrite
```

如果输入文件位于待覆盖的输出目录中，操作会被拒绝；请改用独立输出目录。

## 鼓声、人声或泛音造成误检

先保留原始结果，再从候选缓存生成不同版本：

```powershell
target\debug\glt.exe filter output `
  --output output-auto `
  --auto
```

也可以尝试 `--preset balanced` 或 `--preset melody`。在 TUI 结果页按 `F` 可以编辑
confidence、时长、力度和原始 MIDI 音高范围。每组条件使用 AND，组间使用 OR。

如果鼓点与旋律的特征完全重叠，仅靠音符属性无法可靠区分；后续混音分离属于更高的功能范围。

## 结果太稀疏、弱音或和声被删除

- 将 `--cleaning-profile` 改为 `solo` 或 `auto`，并降低 `--min-confidence`。
- 降低 `--min-duration-ms`，或减小 `--retrigger-gap-ms`。
- 使用 `--arrangement off` 保留更多同时起的音符。
- 在筛选编辑器中使用 `--preset off`，只做结构清理，不做属性筛选。

清理和编排删减会分别写入 `CLEANING_CONFIG`、`ARRANGEMENT_LOSS` 等告警，可用报告核对。

## 节奏过快、过慢或不自然

`--timing auto` 在低置信时保留原始时间，不会强行伪造节拍。可尝试：

```powershell
--timing preserve
--timing straight --bpm 120
--timing triplet --bpm 90
```

手动 BPM 适合已知速度的曲目。复杂变速、连音或自由速度素材可能需要保留原始时序。

## 没有 preview.wav

预览文件只在转换时显式启用：

```powershell
--preview-wav
```

空事件谱不会生成静音文件，报告会写入 `EMPTY_PREVIEW`。已有结果缺少预览时，需要重新执行
转换或筛选，并启用预览生成。

## 无法播放试听

播放设备不可用不会破坏转换结果。可以：

- 直接打开结果目录中的 `preview.wav` 使用外部播放器试听。
- 使用 `glt preview RESULT_DIR --volume 0..1` 重试。
- 检查系统默认输出设备，以及 WAV 文件是否仍然存在。

如果出现 `Symphonia decoder returned an error`，通常是旧结果中的 24-bit WAV（尤其是
`instrumental.wav` 或 `routed-mix.wav`）不被内置播放器直接支持。桌面端会在播放前通过
随包 FFmpeg 自动转换为 44.1 kHz stereo PCM 16-bit；基础包不会调用系统 FFmpeg。新分离和
路由结果也直接输出 PCM 16-bit。旧结果无需重新转录，重新点击播放即可触发热转码缓存。

## 安装或重装 Demucs 分离组件

Demucs、PyTorch 和模型体积较大，不进入基础 GUI/CLI 包，也不会在后台静默下载。离线组件 ZIP
由仓库的 `Separator component` 工作流生成，或使用本机构建目录中的：

```text
glt-local-full-separator-component-windows-x64.zip
```

从桌面端安装：

1. 启动 GenshinLyreTranscriber。
2. 左侧 `SEPARATOR COMPONENT` 点击“安装 / 修复组件”；未安装时“分离与路由”区域也会显示安装入口。
3. 选择上述 ZIP，等待显示 `Demucs 4.1.0` 和 `htdemucs`。

默认安装目录为：

```text
%APPDATA%\com.quyansiyuanwang.genshinlyretranscriber\separator
```

组件包含运行时哈希、模型清单和许可证，安装时会逐项校验；损坏或被修改的 ZIP 会被拒绝。

也支持便携组件：将完整组件 ZIP 解压到主程序旁的 `separator` 文件夹，确保
`separator/separator-component-v1.json` 与 `separator/worker/glt-separator-worker.exe`
都存在，重启后优先使用此目录。界面左侧会显示实际使用的组件路径。
不要只复制 exe；还需要 `_internal`、模型与许可证。

如果出现 `separator file is missing`，展开左侧组件错误详情检查实际路径，使用完整组件 ZIP
重新安装。某些受打包宿主管理的进程会重定向 AppData，导致辅助工具与独立启动的软件看到
不同文件；这种情况下可使用非 AppData 路径下的便携组件。不要关闭路径或哈希校验来绕过错误。

如果出现 `unknown field workerPath`、`stemSet` 或 `maxVoices`，这是旧桌面端请求字段错误，
不是模型缺失。请更新 GUI；无需重新下载模型。分离和路由的重复操作会选择新的输出目录，
不覆盖已有结果。


## 兼容谱与精确谱听起来不同

`score.readable.txt` 只用于人工阅读；`score.compat.txt` 使用 10ms 近似网格。
同一 10ms 槽内的同键重触发无法表示，尾部静音也不能完整表达。需要精确起音时间时使用
`score.events.json`，差异统计见 `report.json`。

## 旧结果无法重新筛选

只有包含 `score.candidates.json` 的新结果可以执行 `glt filter`。旧结果没有候选缓存时，
请重新转录。工具不会通过重新运行模型伪造“快速重筛”。

## TUI 显示或输入异常

- 建议终端窗口至少为 80x24，常用尺寸为 120x40。
- 使用 `F2` 选择文件，或把文件从资源管理器拖入终端。
- 如果终端不支持 bracketed paste，可先粘贴去掉引号的绝对路径。
- 中文或长路径显示不完整时调整窗口宽度；结果详情支持上下滚动。

## 性能或内存问题

Basic Pitch 需要加载 ONNX Runtime、NumPy、SciPy 和 Numba 运行库，首次启动明显慢于后续
处理。长音频会按固定窗口分段，但仍应留意可用内存和临时磁盘空间。若性能明显异常，请记录
输入时长、CPU/内存、worker 版本和 `report.json`，不要上传未授权的原始媒体。
