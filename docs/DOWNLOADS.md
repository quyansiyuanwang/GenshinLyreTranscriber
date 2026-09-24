# 制品下载

项目当前使用 GitHub Actions artifacts 分发 Nightly 和 Debug 构建。它不会自动创建
`v*` Git tag 或 GitHub Release；正式发布必须经过单独的版本与提交审批。

## 从网页下载

1. 打开仓库的 [Nightly workflow](https://github.com/quyansiyuanwang/GenshinLyreTranscriber/actions/workflows/nightly.yml)。
2. 选择最近一次绿色勾选的运行。
3. 在页面底部找到 **Artifacts**。
4. 下载 `glt-nightly-windows-x64-日期-提交`。
5. 解压后先核对 `SHA256SUMS`，再运行其中的 `glt.exe`。

如果最新运行显示成功但没有 Artifact，通常表示该提交已经存在同 SHA 的有效制品，工作流
跳过了重复构建。请继续查看更早的成功运行，优先选择同一提交 SHA、制品尚未过期的记录。

## 使用 GitHub CLI 下载

先确认已登录：

```powershell
gh auth login
```

列出 Nightly 运行：

```powershell
gh run list --workflow nightly.yml --limit 10
```

下载指定运行中的全部制品：

```powershell
gh run download RUN_ID --dir .\nightly
```

只下载一个命名制品：

```powershell
gh run download RUN_ID `
  --name "glt-nightly-windows-x64-YYYYMMDD-SHORT_SHA" `
  --dir .\nightly
```

其中 `RUN_ID` 来自 `gh run list` 的输出，制品名称可以从运行页面的 Artifacts 区域查看。

## 手动触发

Nightly 默认每天北京时间 04:00 运行。也可以手动触发：

```powershell
gh workflow run nightly.yml --ref main
gh run list --workflow nightly.yml --limit 5
```

构建完成后按上面的步骤下载。按当前策略，Nightly 与 Debug 制品保留 14 天。

## 制品内容与边界

Nightly 制品用于测试和诊断，通常包含：

- `glt.exe`
- `glt-worker/`
- `README.md`、`docs/` 和 `LICENSE`
- `SHA256SUMS`

它不是正式离线发行包。音频/视频转录仍可能要求可用的 FFmpeg/ffprobe，正式发行包应把
经许可核查的媒体工具、模型和所有通知完整打包。系统要求、已知限制和卸载方式以正式
Release 说明为准。

## 创建正式 tag 的条件

只有用户明确批准版本号、目标提交和发布方式后，才可以创建正式 `v*` tag 或 GitHub
Release。Nightly 成功不会自动升级为正式版本。
