# 协议 Schema 与版本策略

本目录保存 GenshinLyreTranscriber v1 正式数据契约。当前状态为 `frozen`：

- `events-v1.schema.json`：交给播放器消费的精确起音事件。
- `worker-v1.schema.json`：Rust 与 Python worker 之间的 JSONL 请求和响应。
- `note-sequence-v1.schema.json`：内部统一音符时间轴。
- `report-v1.schema.json`：转换报告与产物清单。
- `versions.json`：正式 Schema 的 SHA256 冻结清单。

## 时间与版本

所有时间字段使用整数微秒，最大值为 `9007199254740991`。JSON boolean 不能作为整数。
v1 顶层版本字段固定为 `1`；未知版本必须报告 `UNSUPPORTED_VERSION`，不能用 v1
解析器猜测字段语义。v1 禁止未声明字段，增加字段或改变语义必须创建新版本。

## 验证层次

JSON Schema 负责类型、范围、枚举、未知字段和组合约束。跨事件顺序、事件不晚于
`duration_us`、NoteSequence 排序、音符范围及产物相对路径等必须继续执行语义验证。
Schema 通过不代表音乐结果通过。

通用错误代码：

- `INVALID_JSON`：语法错误、重复对象键或非有限数字。
- `UNSUPPORTED_VERSION`：版本不受支持。
- `SCHEMA_INVALID`：结构、类型、范围或未知字段错误。
- `EVENT_ORDER`：事件时间未严格递增。
- `DURATION_RANGE`：事件超过片段时长。
- `NOTE_ORDER`：内部音符未按约定排序。
- `NOTE_RANGE`：音符起止、力度或置信度非法。
- `UNSAFE_PATH`：产物路径为绝对路径或逃逸结果目录。

## 共享 fixtures

Rust 与 Python 测试共用：

```text
tests/fixtures/protocol/v1/
```

`cases.json` 包含结构、语义、同刻和弦、版本拒绝、安全整数和内部结构案例；原始
JSON 边界放在 `raw/`。测试命令：

```powershell
cargo test --locked
uv run --directory python pytest
```

播放器交接副本必须逐字节复制本目录中的四份 Schema 与 `versions.json`，并以
`versions.json` 中的 SHA256 验证一致。
