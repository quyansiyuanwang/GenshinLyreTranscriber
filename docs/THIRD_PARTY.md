# 第三方组件与许可

项目源码采用 MIT；随包分发的模型、FFmpeg 和 Python 组件继续适用各自许可证，
不能用项目 MIT 许可证替代。

## Basic Pitch 与 ONNX 模型

- 组件：`basic-pitch 0.4.0`
- 来源：<https://github.com/spotify/basic-pitch/tree/9991303bba609a3b93089d13ec80d1d495083596>
- 代码及模型许可：Apache License 2.0
- 模型：`nmp.onnx`
- SHA256：`2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec`
- 分发要求：保留许可证与 NOTICE，标明修改；本项目不声称得到 Spotify 背书。

## ONNX Runtime

- 版本：1.30.0
- 来源：<https://github.com/microsoft/onnxruntime>
- 许可：MIT

## FFmpeg

- 版本：`n8.1.3-20260923`
- 构建：BtbN FFmpeg-Builds `autobuild-2026-09-23-14-55`
- 构建脚本提交：`ccbffa4f85d0e8de5c135c69ebb10e4c14911fa9`
- FFmpeg 源码版本：`n8.1.3`，提交 `23151b11c75aa44d9ab8db796a53c76acf00f6c0`
- 资产：`ffmpeg-n8.1.3-win64-lgpl-shared-8.1.zip`
- SHA256：`60a055792e88524db437a78c2fcd4471536abf3b30d34fae1249591c00e13432`
- FFmpeg 许可：LGPL v3，采用 shared DLL 构建

正式包必须附带该构建内的 `LICENSE.txt`、可取得对应源码的明确指引，并复核
所有启用库的独立通知。FFmpeg 官方源码可由上述源码 tag 获取，构建脚本和
依赖定义为 BtbN 仓库对应 tag 的源码；未完成这些材料前不得把包标记为可正式发布。

## 其他 Python 依赖

NumPy、SciPy、librosa、pretty_midi、resampy、mir_eval、numba、scikit-learn
及传递依赖由 `python/uv.lock` 固定。打包候选必须生成完整组件清单与许可文本。
