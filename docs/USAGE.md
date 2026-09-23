# 命令行使用

当前 Rust 前端已提供命令解析、worker 启动、JSONL 状态处理、取消和退出码。正式
worker 已支持从本地音频/视频提取音轨并生成 `source.mid` 与基础 `report.json`；
清理、节奏分析、琴键映射、完整报告和三类谱导出仍在后续模块中实现。

```powershell
glt --help
glt doctor [--worker PATH] [--json]
glt transcribe INPUT --output DIR [OPTIONS]
glt convert-midi INPUT --output DIR [OPTIONS]
```

## 常用参数

- `--timing auto|preserve|straight|triplet`
- `--bpm NUMBER`，仅音频转录使用
- `--transpose auto|INTEGER`
- `--audio-track N`
- `--start-seconds SECONDS`、`--end-seconds SECONDS`
- `--preview-wav`
- `--overwrite`
- `--json`
- `--worker PATH`，仅开发或高级诊断使用

没有传入 `--worker` 时，程序读取 `GLT_WORKER_PATH`，然后查找与主程序相邻的
`glt-worker` 目录。程序不会通过 shell 拼接输入路径。

源码开发时可直接选择 Python 入口：

```powershell
glt transcribe input.mp4 --output output --worker python/src/glt_core/worker.py
```

## 输出与退出码

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
实际转谱输入、质量与输出说明将在对应处理模块完成后补充。
