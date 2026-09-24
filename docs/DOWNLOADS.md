# Nightly 下载

项目采用与 RustDesk 相同的固定 Nightly tag 模型：

- Git tag：`nightly`
- GitHub Release：`Nightly`
- Release 类型：Pre-release
- Windows 资产：`glt-nightly-windows-x64.zip`
- 校验资产：`glt-nightly-windows-x64.sha256`

每天定时构建或手动触发成功后，workflow 会创建 `nightly` tag（如果尚不存在）并更新同名
Pre-release 的资产。稳定版本仍使用独立的 `v*` tag，不会自动创建。

## 从 Releases 下载

打开 [Nightly Pre-release](https://github.com/quyansiyuanwang/GenshinLyreTranscriber/releases/tag/nightly)，
在 **Assets** 中下载：

- `glt-nightly-windows-x64.zip`
- `glt-nightly-windows-x64.sha256`

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
  --pattern "glt-nightly-windows-x64.zip" `
  --dir .\nightly
```

## 校验与解压

先在 PowerShell 中计算 ZIP 哈希：

```powershell
Get-FileHash .\nightly\glt-nightly-windows-x64.zip -Algorithm SHA256
```

输出应与 `glt-nightly-windows-x64.sha256` 中的值一致，然后解压并运行 `glt.exe`。

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

Nightly ZIP 当前包含：

- `glt.exe`
- `glt-worker/`
- `README.md`、`docs/` 和 `LICENSE`
- `SHA256SUMS`

Nightly 用于测试最新 `main`，不是正式离线发行包。音频/视频转录仍可能要求可用的
FFmpeg/ffprobe；稳定发布包会额外完成媒体工具、模型、许可证、断网和干净 Windows 验收。

## 稳定版本

正式 `v*` tag 和稳定 Release 必须由维护者明确批准版本号和目标提交后创建。Nightly 成功不会
自动升级为稳定版本。
