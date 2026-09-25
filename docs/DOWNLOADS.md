# Nightly 下载

项目采用与 RustDesk 相同的固定 Nightly tag 模型：

- Git tag：`nightly`
- GitHub Release：`Nightly`
- Release 类型：Pre-release
- GUI 安装器：`glt-nightly-gui-windows-x64-setup.exe`
- GUI 便携包：`glt-nightly-gui-windows-x64.zip`
- CLI/TUI 便携包：`glt-nightly-cli-tui-windows-x64.zip`
- 校验清单：`glt-nightly-SHA256SUMS`

每天定时构建或手动触发成功后，workflow 会创建 `nightly` tag（如果尚不存在）并更新同名
Pre-release 的资产；固定 `nightly` tag 会以 lease 保护的强制更新移动到对应成功构建的提交。
稳定版本仍使用独立的 `v*` tag，不会自动创建。

## 从 Releases 下载

打开 [Nightly Pre-release](https://github.com/quyansiyuanwang/GenshinLyreTranscriber/releases/tag/nightly)，
在 **Assets** 中下载：

- `glt-nightly-gui-windows-x64-setup.exe`
- `glt-nightly-gui-windows-x64.zip`
- `glt-nightly-cli-tui-windows-x64.zip`
- `glt-nightly-SHA256SUMS`

也可以从仓库首页右侧 Releases 区域进入 `Nightly`。

## 使用 GitHub CLI 下载

下载全部 Nightly 资产：

```powershell
gh release download nightly `
  --repo quyansiyuanwang/GenshinLyreTranscriber `
  --dir .\nightly
```

只下载 ZIP：

```powershell
gh release download nightly `
  --repo quyansiyuanwang/GenshinLyreTranscriber `
  --pattern "glt-nightly-gui-windows-x64.zip" `
  --dir .\nightly
```

## 校验与解压

先在 PowerShell 中计算 ZIP 哈希：

```powershell
Get-FileHash .\nightly\glt-nightly-gui-windows-x64.zip -Algorithm SHA256
```

输出应与 `glt-nightly-SHA256SUMS` 中对应文件的值一致。安装器可直接运行；便携 GUI ZIP
解压后启动 `GenshinLyreTranscriber.exe`；安装版从开始菜单启动，CLI/TUI ZIP 解压后运行
`glt.exe`。

## 手动触发

Nightly 默认每天北京时间 04:00 运行，也可以手动触发：

```powershell
gh workflow run nightly.yml --ref main
gh run list --workflow nightly.yml --limit 5
```

构建成功后，[Nightly Pre-release](https://github.com/quyansiyuanwang/GenshinLyreTranscriber/releases/tag/nightly)
资产会自动更新。GitHub Actions artifact 也会保留 14 天，便于查看构建诊断，但正式下载入口是
上面的 Release 资产。

## 包内容与边界

Nightly 当前提供三个 Windows x64 资产：NSIS 安装器、GUI 便携 ZIP 和 CLI/TUI 便携 ZIP。
每个包都包含匹配的 `glt-worker/`、固定 LGPL FFmpeg/ffprobe、`README.md`、`docs/` 和
`LICENSE`。统一
`glt-nightly-SHA256SUMS` 校验最终压缩包/安装器。

可选 Demucs 分离运行时体积较大，不进入基础包，由独立组件工作流构建并记录模型、运行时和
许可证校验信息。

需要分离组件时，维护者可手动触发 `Separator component` 工作流：

```powershell
gh workflow run separator-component.yml `
  -f ref=main `
  -f publish_to_nightly=true
```

组件 ZIP 与独立 SHA256 文件会附加到 `nightly` Pre-release。首次安装时，在桌面端
“分离与路由”区域选择该 ZIP；程序只读取本地包，不会静默下载模型。

Nightly 用于测试最新 `main`，不是正式离线发行包。音频/视频转录仍可能要求可用的
FFmpeg/ffprobe；稳定发布包会额外完成媒体工具、模型、许可证、断网和干净 Windows 验收。

## 稳定版本

正式 `v*` tag 和稳定 Release 必须由维护者明确批准版本号和目标提交后创建。Nightly 成功不会
自动升级为稳定版本。
