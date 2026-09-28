// Run against a WebView2/Chromium instance started with --remote-debugging-port.
// The dev server must already be running. This navigates only the selected test page.
const port = Number(process.argv[2] ?? 9238);
const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
const page = pages.find((entry) => entry.type === "page");
if (!page) throw new Error("No browser page available for DOM regression");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
let sequence = 0;
const pending = new Map();
ws.addEventListener("message", ({ data }) => {
  const message = JSON.parse(data);
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  message.error ? request.reject(message.error) : request.resolve(message.result);
});
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
const timeout = setTimeout(() => { console.error("Browser regression timed out"); process.exit(1); }, 30_000);
try {
  await rpc("Page.navigate", { url: "http://127.0.0.1:1420/tests/browser/playback.html" });
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const result = await rpc("Runtime.evaluate", { expression: "window.regression", returnByValue: true });
    if (!result.result?.value) continue;
    console.log(JSON.stringify(result.result.value, null, 2));
    if (!result.result.value.ok) process.exitCode = 1;
    break;
  }
} finally { clearTimeout(timeout); ws.close(); }
