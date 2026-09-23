# 命令行使用

当前 Rust 前端已提供命令解析、worker 启动、JSONL 状态处理、取消和退出码。完整
音频处理 worker 尚未接入，因此除 `doctor` 外，`transcribe` 和 `convert-midi`
需要显式指向符合正式 worker v1 协议的开发 worker。

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
