# 播放隔离的真实 DOM 回归

这里不是静态 HTML 快照。`playback.html` 在真实浏览器中挂载 5000 音符的
钢琴卷帘与分析画布，检查播放时 React 提交/静态绘制次数、指针移动和暂停、
尺寸变化、焦点键盘操作、撤销，以及隐藏分析面板的可见性回调。

1. 在 GUI 目录运行 `npm run dev`。
2. 启动专用测试 WebView2/Chromium，并启用 `--remote-debugging-port=9238`。
   不要连接用户正在编辑工程的窗口：测试会导航该实例的第一个页面。
3. 在 GUI 目录运行 `node scripts/browser-regression.mjs 9238`。
   失败或超时返回非零；成功输出 JSON 断言及播放阶段计数。

原生 GUI 的 WebView2 可以通过 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 注入调试参数。
测试后关闭该专用进程并移除环境变量。此页面不进入产品入口或发布包。

本测试使用模拟播放时钟，不证明音频后端时间精度，也不替代安装版全流程、
60 秒实播或 10 分钟长素材性能门禁。
