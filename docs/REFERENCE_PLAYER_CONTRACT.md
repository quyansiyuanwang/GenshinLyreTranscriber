# 参考播放器兼容契约

本项目不会修改 `GIPianoPlayer`。兼容谱的验证基线固定为：

- 远端：`https://github.com/quyansiyuanwang/GenshinImpactPianoPlayer.git`
- 提交：`7aaddad21b71e91fd36b6ce7687dfb546076f602`
- 验证对象：`src/core/parser/score_parser.py` 与 `src/core/player/player.py`
- 验证方式：读取指定检出目录，在虚拟时钟和虚拟键盘上执行，不发送系统按键

## 已核验的解析行为

- 正文行会先执行 `strip()`，所以前导空格不能直接表示前导休止。
- 配置文件以 `---` 结束；正文中的 `#` 注释会被跳过。
- `/` 只是视觉分隔符，不消耗时间。
- 单音、和弦和空格都代表一个正文槽；默认槽长由 `INTERVAL_RATING` 控制。
- 行内槽之后等待，正文最后一槽结束后不等待，因此尾部静音不能由旧格式完整表达。

## 兼容谱约束

- `INTERVAL_RATING = 0.01`，其余间隔和段设置由导出器写入固定值。
- 正文按每行 4 个斜杠段组织，每段 4 个 10ms 槽；每行首尾都有 `/`，确保 `strip()` 不会删除前导空格。
- `LINE_INTERVAL_RATING = 1.0` 补回换行边界的一个间隔，保证跨行起音仍精确。
- 起音使用整数微秒按最近 10ms 取整，5000us 平局向上。
- 槽内不同按键可以合并为和弦；同一键的重触发不能被和弦表达，必须报告碰撞。
- 不使用琶音编码变速，不在正文中加入内部换行或装饰空格。

## 验证命令

```powershell
uv run --project python python scripts/verify_reference_player_contract.py `
  --player-root PATH_TO_GIPIOPLAYER `
  --compat-score RESULT/score.compat.txt `
  --events-json RESULT/score.events.json `
  --report reference-contract.json
```

不传 `--compat-score` 和 `--events-json` 时使用内置前导休止、相邻槽与和弦样例。
脚本记录参考提交、解析器/播放器源码 SHA256、解析告警和逐槽虚拟调度结果。
