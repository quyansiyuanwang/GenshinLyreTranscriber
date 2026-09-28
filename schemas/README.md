# 协议 Schema 与版本策略

本目录保存事件 v1、NoteSequence v1、候选缓存 v1、Performance v1，以及 worker/report v3 正式数据契约。
当前状态为 `frozen`：

- `events-v1.schema.json`：交给播放器消费的精确起音事件。
- `worker-v1.schema.json`：Rust 与 Python worker 之间的 JSONL 请求和响应。
- `note-sequence-v1.schema.json`：内部统一音符时间轴。
- `report-v1.schema.json`：转换报告与产物清单。
- `candidate-cache-v1.schema.json`：结果页快速重筛所需候选音符和 timing 元数据。
- `worker-v2.schema.json`：v2 JSONL worker，新增 `refilter` operation、FilterSpec 和预设/自动检测选项。
- `report-v2.schema.json`：v2 报告，新增筛选规格、筛选统计和候选缓存 artifact。
- `performance-v1.schema.json`：可编辑映射表演层，保留来源 stem、velocity、confidence 和 Pitch Bend 参考。
- `worker-v3.schema.json`：v3 JSONL worker，新增从既有 Performance 生成不可变派生产物的 operation。
- `report-v3.schema.json`：v3 报告，记录 Performance 与离散 MIDI 派生产物。
- `versions.json`：正式 Schema 的 SHA256 冻结清单。

## 时间与版本

所有时间字段使用整数微秒，最大值为 `9007199254740991`。JSON boolean 不能作为整数。
worker/report v3 顶层版本字段固定为 `3`；事件、NoteSequence 和 Performance 保持 v1。未知版本必须
报告 `UNSUPPORTED_VERSION`，不能用旧解析器猜测字段语义。各版本禁止未声明字段。

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

播放器交接继续使用冻结的 `events-v1.schema.json`；worker/report v3、Performance 和候选缓存只属于
本工具内部及结果页重筛。所有 Schema 均以 `versions.json` 的 SHA256 验证一致。

## 桌面取消事件 v1

`desktop-cancellation-v1.schema.json` 定义分析、分离与路由的取消终态；仅在整个受管进程树
回收完成后发出。点击取消不等同于收到此终态。此契约不改变 worker JSONL 或播放器事件格式。
Rust 与前端共用 `tests/fixtures/desktop-cancellation-v1.json`，字段包括版本、操作和取消状态。


## 桌面恢复草稿 v1

`edit-draft-v1.schema.json` 是应用私有恢复数据，不是正式修订或播放器交接产物。
它引用冻结的 Performance v1，记录源结果、基准 revision/SHA256、更新时间及选择/视口/工具状态。
Rust、TypeScript 和 Python 共用 `tests/fixtures/edit-draft-v1.json`。worker JSONL、events-v1、
NoteSequence-v1 均不因此升级。草稿仅保存到应用数据目录；公开工程和导出不包含本地草稿路径。

桌面加载/保存响应标识 `format_version: 1`。保存与删除使用先前读取的草稿内容 SHA256 做冲突检测；
跨进程文件锁保护校验到发布，进程退出时由操作系统释放。先落盘并同步 staging，再原子替换，
不通过覆盖式复制伪造原子发布。Windows 路径重定向场景从实际锁文件定位物理数据目录，保证同卷发布。
