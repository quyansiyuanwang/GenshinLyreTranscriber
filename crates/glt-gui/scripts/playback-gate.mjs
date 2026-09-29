// Read-only CDP instrumentation for a dedicated native test window. It never
// replaces invoke, requestAnimationFrame or the application's playback clock.
import { readFileSync, writeFileSync, existsSync, cpSync, mkdtempSync, lstatSync } from "node:fs";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import { createHash } from "node:crypto";
import { summarizeGate } from "./playback-gate-metrics.mjs";

const [resultArg, output, portArg = "9240", secondsArg = "615", label = "candidate"] = process.argv.slice(2);
if (!resultArg || !output) throw new Error("Usage: node scripts/playback-gate.mjs RESULT_DIR NEW_REPORT_JSON [CDP_PORT] [SECONDS] [LABEL]");
if (existsSync(output)) throw new Error("Refusing to replace an existing measurement report");
const seconds = Number(secondsArg);
if (!Number.isFinite(seconds) || seconds < 60) throw new Error("Measure at least 60 seconds");
const resultDir = resolve(resultArg);
const document = JSON.parse(readFileSync(join(resultDir, "performance.json"), "utf8"));
const reloadResult = join(mkdtempSync(join(tmpdir(), "glt-resource-gate-")), "result");
cpSync(resultDir, reloadResult, { recursive: true, filter: path => !lstatSync(path).isSymbolicLink() });
const datasetHash = createHash("sha256").update(readFileSync(join(resultDir, "performance.json"))).digest("hex");
const pages = await fetch(`http://127.0.0.1:${Number(portArg)}/json/list`).then((r) => r.json());
const page = pages.find((p) => p.type === "page");
if (!page) throw new Error("No dedicated native page found");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((ok, fail) => { ws.addEventListener("open", ok, { once: true }); ws.addEventListener("error", fail, { once: true }); });
let id = 0;
const pending = new Map();
function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    const key = ++id;
    const timer = setTimeout(() => { pending.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 20_000);
    pending.set(key, { resolve, reject, timer }); ws.send(JSON.stringify({ id: key, method, params }));
  });
}
async function evaluate(expression) {
  const reply = await rpc("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (reply.exceptionDetails) throw new Error(JSON.stringify(reply.exceptionDetails));
  return reply.result.value;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(expression, limit = 30_000) {
  const end = Date.now() + limit;
  while (Date.now() < end) { const value = await evaluate(expression); if (value) return value; await sleep(100); }
  throw new Error(`UI timeout: ${expression}`);
}
async function click(selector) {
  await evaluate(`document.querySelector(${JSON.stringify(selector)})?.scrollIntoView({block:'center'})`);
  const box = await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e||e.disabled)throw new Error('control unavailable');return e.getBoundingClientRect().toJSON()})()`);
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2, button: "left", clickCount: 1 };
  await rpc("Input.dispatchMouseEvent", { type: "mousePressed", ...point });
  await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", ...point });
}
async function clickText(text) {
  const selector = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}&&e.getClientRects().length&&e.checkVisibility());if(!b)throw new Error('missing button');b.dataset.gateTarget='button';return 'button[data-gate-target="button"]';})()`);
  await click(selector); await evaluate("document.querySelector('[data-gate-target]')?.removeAttribute('data-gate-target')");
}
async function setInput(selector, value) {
  await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw new Error('missing input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(String(value))});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
}
let phase = "setup", latest = null, sourceDuration = 0, timeOrigin = 0;
const requests = new Map(), live = {}, maxLive = {}, counts = {}, rpcFailures = [], requestedStarts = [];
let firstPosition = null, lastPosition = null, statusReads = 0;
let previousSteady = null, continuityChecks = 0;
const discontinuities = [];
let preferences = null;
ws.addEventListener("message", async ({ data }) => {
  const message = JSON.parse(data);
  const call = pending.get(message.id);
  if (call) { pending.delete(message.id); clearTimeout(call.timer); message.error ? call.reject(message.error) : call.resolve(message.result); return; }
  if (message.method === "Network.requestWillBeSent") {
    const p = message.params; const url = new URL(p.request.url);
    if (url.hostname !== "ipc.localhost" || p.request.method !== "POST") return;
    const command = url.pathname.slice(1);
    requests.set(p.requestId, { command, at: p.timestamp, wall: p.wallTime, phase });
    live[command] = (live[command] ?? 0) + 1;
    if (phase === "steady") { counts[command] = (counts[command] ?? 0) + 1; maxLive[command] = Math.max(maxLive[command] ?? 0, live[command]); }
    if (command === "play_ab_source") {
      try { requestedStarts.push(JSON.parse(p.request.postData).positionUs); } catch { /* no payload inspection beyond the numeric start position */ }
    }
  }
  if (["Network.loadingFinished", "Network.loadingFailed"].includes(message.method)) {
    const request = requests.get(message.params.requestId); if (!request) return;
    requests.delete(message.params.requestId); live[request.command]--;
    if (message.method === "Network.loadingFailed") { rpcFailures.push({ command: request.command, phase: request.phase }); return; }
    if (request.command === "playback_status") {
      try {
        const body = await rpc("Network.getResponseBody", { requestId: message.params.requestId });
        const value = JSON.parse(body.body);
        const elapsed = (message.params.timestamp - request.at) * 1000;
        const sample = { ...value, anchorMs: request.wall * 1000 - timeOrigin + elapsed / 2, rttMs: elapsed };
        if (!latest || sample.anchorMs >= latest.anchorMs) latest = sample;
        if (value.available && !value.paused) { firstPosition ??= value.position_us; lastPosition = value.position_us; }
        if (request.phase === "steady" && sample.available && !sample.paused && sample.position_us < sourceDuration - 100_000) {
          if (previousSteady && sample.anchorMs > previousSteady.anchorMs) {
            const deviationMs = (sample.position_us - previousSteady.position_us) / 1000 - (sample.anchorMs - previousSteady.anchorMs);
            continuityChecks++;
            if (Math.abs(deviationMs) > 250) discontinuities.push(deviationMs);
          }
          previousSteady = sample;
        }
        statusReads++;
      } catch (error) { rpcFailures.push({ command: "status-response-inspection", reason: error instanceof Error ? error.message : JSON.stringify(error) }); }
    }
  }
});

const report = { format_version: 1, label, dataset: { sha256: datasetHash, notes: document.notes.length, duration_us: document.duration_us }, started_at: new Date().toISOString(), resources: [], positions: [], excludedPositionSamples: 0 };
const outerTimeout = setTimeout(() => { console.error("Performance run timed out"); process.exit(1); }, (seconds + 180) * 1000);
try {
  await rpc("Network.enable", {maxTotalBufferSize:1048576,maxResourceBufferSize:65536,maxPostDataSize:1024}); await rpc("Performance.enable");
  timeOrigin = await evaluate("performance.timeOrigin");
  preferences = await evaluate("Object.fromEntries(['glt.workspace-layout.v1','glt.recentOutputs'].map(k=>[k,localStorage.getItem(k)]))");
  if (!(await evaluate("location.protocol==='http:'&&location.hostname==='tauri.localhost' || location.protocol==='tauri:'"))) throw new Error("Performance gates require a bundled native frontend, not devUrl");
  await setInput('input[placeholder="选择或拖入文件"]', resultDir);
  await clickText("开始转换");
  await until("!!document.querySelector('.piano-roll')");
  if (await evaluate("!!document.querySelector('[role=tab][aria-controls=view-editor]')")) await click('[role=tab][aria-controls=view-editor]');
  // The result's synthesized WAV is the same 10-minute source for both builds.
  sourceDuration = await evaluate(`window.__TAURI_INTERNALS__.invoke('probe_media',{input:${JSON.stringify(join(resultDir, "preview.wav"))},workerPath:null}).then(v=>v.duration_us)`);
  await setInput(".volume-control input", 0);
  await evaluate("window.__TAURI_INTERNALS__.invoke('stop_preview')"); await sleep(200);
  await click(".ab-slots button:nth-child(2)");
  const waiting = Date.now(); while ((!latest?.available || latest.paused) && Date.now() - waiting < 30_000) await sleep(100);
  if (!latest?.available || latest.paused) throw new Error("Playback did not start");
  await evaluate("document.querySelector('.piano-canvas-wrap').scrollIntoView({block:'center'})");
  await sleep(1200);
  report.viewport = await evaluate("({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,hidden:document.hidden})");
  report.viewport.nativeVisible = await evaluate("window.__TAURI_INTERNALS__.invoke('plugin:window|is_visible',{label:'main'})");
  if (report.viewport.hidden || !report.viewport.nativeVisible) throw new Error("Window is not visible; off-screen rendering cannot pass this gate");
  await evaluate(`(()=>{
    const b=window.__gltGate={phase:'warmup',frames:[],longTasks:[],interactions:[],previous:performance.now(),running:true};
    function frame(t){if(!b.running)return;if(b.phase==='steady'&&b.previous!==null)b.frames.push(t-b.previous);b.previous=t;requestAnimationFrame(frame)}requestAnimationFrame(frame);
    b.longObserver=new PerformanceObserver(list=>{if(b.phase==='steady')b.longTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration})))});b.longObserver.observe({entryTypes:['longtask']});
    b.input=e=>{if(!e.isTrusted||b.phase!=='churn')return;const target=e.target;if(!(target instanceof Element)||!target.closest('button,canvas,input'))return;const at=performance.now();requestAnimationFrame(()=>requestAnimationFrame(()=>b.interactions.push({type:e.type,ms:performance.now()-at})));};
    for(const event of ['click','input','pointermove'])document.addEventListener(event,b.input,true);
  })()`);
  await rpc("HeapProfiler.collectGarbage");
  report.resources.push({ phase: "start", dom: await rpc("Memory.getDOMCounters"), metrics: (await rpc("Performance.getMetrics")).metrics, heapUsage: await rpc("Runtime.getHeapUsage") });
  await evaluate("window.__TAURI_INTERNALS__.invoke('seek_playback',{positionUs:0})");
  await sleep(100);
  report.zeroSeekVerified = latest?.position_us < 250_000;
  await evaluate("window.__gltGate.previous=null;window.__gltGate.phase='steady'");
  phase = "steady";
  const started = Date.now(); let nextPan = 0, nextResource = 60_000;
  while (Date.now() - started < Math.min(seconds * 1000, sourceDuration / 1000 + 1500)) {
    const elapsed = Date.now() - started;
    if (elapsed >= nextPan && latest?.available) {
      // Pan is a UI-only viewport change, not an audio seek or time simulation.
      await evaluate(`(()=>{const e=document.querySelector('.piano-lanes input');const span=${document.duration_us}-Number(e.max);const value=Math.max(0,Math.min(Number(e.max),${latest.position_us}-span/2));Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,String(value));e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
      nextPan += 10_000;
    }
    const status = latest;
    const ui = await evaluate(`(()=>{const e=document.querySelector('.piano-playhead-track'),slider=document.querySelector('.piano-lanes input');if(!e||!slider||document.hidden||getComputedStyle(e).visibility==='hidden')return null;const ratio=parseFloat(e.style.transform.slice(11))/100;return {at:performance.now(),position:Number(slider.value)+ratio*(${document.duration_us}-Number(slider.max))};})()`);
    if (ui && Number.isFinite(ui.position) && status?.available && !status.paused && status.rttMs <= 20 && ui.at - status.anchorMs >= 0 && ui.at - status.anchorMs <= 200 && status.position_us < document.duration_us - 300_000) {
      const predicted = Math.min(document.duration_us, status.position_us + (ui.at - status.anchorMs) * 1000);
      report.positions.push({ error_ms: (ui.position - predicted) / 1000, uncertainty_ms: status.rttMs / 2 });
    } else report.excludedPositionSamples++;
    if (elapsed >= nextResource) {
      report.resources.push({ phase: `second-${Math.round(elapsed / 1000)}`, dom: await rpc("Memory.getDOMCounters"), metrics: (await rpc("Performance.getMetrics")).metrics, heapUsage: await rpc("Runtime.getHeapUsage") });
      console.log(`${label}: ${Math.round(elapsed / 1000)}s, source ${(latest?.position_us / 1e6).toFixed(2)}s`); nextResource += 60_000;
    }
    await sleep(97);
  }
  report.measuredSteadySeconds = (Date.now() - started) / 1000;
  report.lastPositionUs = lastPosition;
  phase = "churn"; await evaluate("window.__gltGate.phase='churn'");
  // Start playback again only after the full-file measurement, for seek recovery.
  const seekAt = performance.now();
  await evaluate("window.__TAURI_INTERNALS__.invoke('seek_playback',{positionUs:120000000})");
  const seekWait = Date.now(); while (Math.abs((latest?.position_us ?? 0) - 120_000_000) > 1_000_000 && Date.now() - seekWait < 3000) await sleep(20);
  report.seekRecoveryMs = performance.now() - seekAt;
  report.seekRecovered = !!latest?.available && !latest.paused && Math.abs(latest.position_us - 120_000_000) <= 1_000_000;
  await rpc("HeapProfiler.collectGarbage");
  report.resources.push({ phase: "before-churn", dom: await rpc("Memory.getDOMCounters"), metrics: (await rpc("Performance.getMetrics")).metrics, heapUsage: await rpc("Runtime.getHeapUsage") });
  const tabs = await evaluate("!!document.querySelector('[role=tab][aria-controls=view-editor]')");
  for (let i = 0; i < 12; i++) {
    if (tabs) { await click('[role=tab][aria-controls=view-filter]'); await until("document.querySelector('[role=tab][aria-controls=view-filter]').getAttribute('aria-selected')==='true'"); await click('[role=tab][aria-controls=view-editor]'); }
    const beforeZoom = await evaluate("Number(document.querySelector('.piano-lanes input').max)");
    await clickText("放大"); await until(`Number(document.querySelector('.piano-lanes input').max)>${beforeZoom}`);
    const afterZoom = await evaluate("Number(document.querySelector('.piano-lanes input').max)");
    await clickText("缩小"); await until(`Number(document.querySelector('.piano-lanes input').max)<${afterZoom}`);
    if (i % 2 === 0) {
      const next = i % 4 === 0 ? reloadResult : resultDir;
      await setInput('input[placeholder="选择或拖入文件"]', next); await clickText("开始转换");
      await until(`document.querySelector('.result-path strong')?.textContent===${JSON.stringify(next)}`);
      await until("!!document.querySelector('.piano-roll')");
      if (tabs) await click('[role=tab][aria-controls=view-editor]');
    }
    await evaluate("document.querySelector('.piano-canvas-wrap').scrollIntoView({block:'center'})");
    await sleep(75);
  }
  if (tabs) await click('[role=tab][aria-controls=view-filter]'); else await clickText("手动筛选");
  await clickText("一键范围");
  await evaluate("document.querySelector('.filter-preview-canvas').scrollIntoView({block:'center'})");
  const box = await evaluate("document.querySelector('.filter-preview-canvas').getBoundingClientRect().toJSON()");
  const minimum = await evaluate("Number([...document.querySelectorAll('.range-row input')].slice(-2)[0].value)");
  const x = box.x + box.width / 2, y = box.y + 8 + (1 - minimum / 127) * (box.height - 30);
  for (let i = 0; i < 8; i++) {
    const fields = await evaluate("[...document.querySelectorAll('.range-row input')].map(e=>e.value).join(',')");
    await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y: y - 9, button: "left", buttons: 1 });
    await until(`[...document.querySelectorAll('.range-row input')].map(e=>e.value).join(',')!==${JSON.stringify(fields)}`);
    await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 });
    await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }); await sleep(40);
  }
  if (tabs) await click('[role=tab][aria-controls=view-editor]'); else await clickText("手动筛选");
  await sleep(1200); await rpc("HeapProfiler.collectGarbage");
  report.resources.push({ phase: "after-churn", dom: await rpc("Memory.getDOMCounters"), metrics: (await rpc("Performance.getMetrics")).metrics, heapUsage: await rpc("Runtime.getHeapUsage") });
  const collected = await evaluate("(()=>{const b=window.__gltGate;b.running=false;b.longObserver.disconnect();for(const event of ['click','input','pointermove'])document.removeEventListener(event,b.input,true);return {frames:b.frames,longTasks:b.longTasks,interactions:b.interactions}})()");
  Object.assign(report, collected, { seconds, sourceDurationUs: sourceDuration, firstPositionUs: firstPosition, requestedStarts, statusReads, continuityChecks, discontinuities, counts, maxInFlight: maxLive, rpcFailures });
  report.gates = summarizeGate(report);
  await evaluate("window.__TAURI_INTERNALS__.invoke('stop_preview')");
  writeFileSync(output, JSON.stringify(report, null, 2), { flag: "wx" });
  console.log(JSON.stringify(report.gates, null, 2));
} catch (error) {
  report.error = String(error); writeFileSync(output, JSON.stringify(report, null, 2), { flag: "wx" }); process.exitCode = 1; console.error(error);
} finally {
  clearTimeout(outerTimeout); phase = "stopped";
  if (preferences) {
    try { await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(preferences)})){if(value===null)localStorage.removeItem(key);else localStorage.setItem(key,value)}})()`); }
    catch { /* a closed test window cannot restore volatile UI preferences */ }
  }
  for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("Measurement closed")); }
  pending.clear(); ws.close();
}
