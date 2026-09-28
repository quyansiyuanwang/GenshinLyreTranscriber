// Run only in a dedicated development WebView with a non-empty published result.
// Edits are exercised in memory and undone; no worker export is started.
import { cpSync, mkdtempSync, lstatSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const inputResultDir = process.argv[2];
if (!inputResultDir) throw new Error("Usage: node scripts/workspace-regression.mjs RESULT_DIR [CDP_PORT]");
const scratch = mkdtempSync(join(tmpdir(), "glt-workspace-regression-"));
const resultDir = join(scratch, "result");
cpSync(inputResultDir, resultDir, { recursive: true, filter: path => !lstatSync(path).isSymbolicLink() });
const port = Number(process.argv[3] ?? 9238);
const pages=await fetch(`http://127.0.0.1:${port}/json/list`).then(r=>r.json());const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));let n=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}});const rpc=(method,params={})=>new Promise((resolve,reject)=>{const id=++n;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});const evaljs=async expression=>{const r=await rpc('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;};const sleep=ms=>new Promise(r=>setTimeout(r,ms));const checks=[];function check(c,label){if(!c)throw new Error(label);checks.push(label);}
const click=label=>evaljs(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)}&&b.getClientRects().length);if(!b||b.disabled)throw new Error('button not available '+${JSON.stringify(label)});b.click();})()`);
const timeout = setTimeout(() => { console.error("Workspace regression timed out"); process.exit(1); }, 30_000);
const previousLayout = await evaljs("localStorage.getItem('glt.workspace-layout.v1')");
try{
 await evaljs(`(()=>{const e=document.querySelector('input[placeholder="选择或拖入文件"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(resultDir)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);await sleep(200);await click('开始转换');await sleep(1200);
 check(await evaljs(`!!document.querySelector('.filter-preview-canvas')&&document.querySelector('[role=tab][aria-selected=true]').textContent==='筛选'`),'existing native result opens filter view');
 const field='document.querySelector(".filter-inspector .range-row input")';
 await evaljs(`(()=>{const e=${field};Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'0.55');e.dispatchEvent(new Event('input',{bubbles:true}));})()`);await sleep(100);
 await click('钢琴卷帘');await sleep(250);
 await evaljs(`(()=>{const e=document.querySelector('.piano-roll');e.focus();e.dispatchEvent(new KeyboardEvent('keydown',{key:'a',ctrlKey:true,bubbles:true}));})()`);await sleep(100);
 const selected=await evaljs(`document.querySelector('.piano-toolbar').innerText`);check(!selected.includes('未选择音符'),'editor selected all notes');
 await evaljs(`document.querySelector('.piano-roll').dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}))`);await sleep(100);
 await click('分析');await click('筛选');await sleep(150);check(await evaljs(`${field}.value==='0.55'`),'filter numeric draft preserved across views');
 await click('钢琴卷帘');await sleep(100);
 await evaljs(`(()=>{const e=document.querySelector('.piano-roll');e.focus();e.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}));})()`);await sleep(100);check(await evaljs(`document.querySelector('.piano-toolbar').innerText`)===selected,'editor history and selection survive view switching');
 await click('开始转换');await sleep(150);await click('放弃草稿与编辑');await sleep(150);
 await click('筛选');
 const layouts=[];
 for(const [width,height,scale] of [[1366,768,1],[1920,1080,1],[1366,768,1.25],[1366,768,1.5],[1920,1080,1.5]]){
  await rpc('Emulation.setDeviceMetricsOverride',{width:Math.round(width/scale),height:Math.round(height/scale),deviceScaleFactor:scale,mobile:false});await sleep(180);
  const result=await evaljs(`(()=>{const keys=[...document.querySelectorAll('.workspace-tabs button,.transport-button,.task-drawer-toggle,.run-bar>.primary-button')];return {width:innerWidth,height:innerHeight,unreachable:keys.filter(e=>{const r=e.getBoundingClientRect();return r.x<0||r.y<0||r.right>innerWidth+.5||r.bottom>innerHeight+.5}).map(e=>e.textContent),overflow:[...document.querySelectorAll('.sidebar,.workspace-inspector,.workspace')].map(e=>({name:e.className,extra:e.scrollWidth-e.clientWidth})),filterBacking:[document.querySelector('.filter-preview-canvas').width,document.querySelector('.filter-preview-canvas').clientWidth*devicePixelRatio]};})()`);
  check(result.unreachable.length===0,`key controls reachable ${width}x${height}@${scale}`);layouts.push({scale,...result});
 }
 await rpc('Emulation.clearDeviceMetricsOverride');await sleep(100);
 await click('资源');await click('检查器');await click('展开任务');await sleep(100);
 check(await evaljs(`(()=>{const p=JSON.parse(localStorage.getItem('glt.workspace-layout.v1'));return p.view==='filter'&&!p.resources&&!p.inspector&&p.tasks;})()`),'collapsed panels and active view persist');
 await rpc('Page.reload');await sleep(1200);check(await evaljs(`document.querySelector('.app-shell').dataset.view==='filter'&&document.querySelector('.sidebar').hidden&&document.querySelector('.workspace-inspector').hidden`),'layout restores after reload');
 await evaljs(`document.querySelectorAll('.daw-menu')[3].open=true`);await click('恢复默认布局');await sleep(150);check(await evaljs(`document.querySelector('.app-shell').dataset.view==='analysis'&&!document.querySelector('.sidebar').hidden&&!document.querySelector('.workspace-inspector').hidden`),'restore default layout works');
 console.log(JSON.stringify({ok:true,checks,layouts},null,2));
} catch (error) { console.error(JSON.stringify({ok:false,checks,error:String(error)},null,2)); process.exitCode=1; }
finally {
 await evaljs(`localStorage.${previousLayout === null ? "removeItem('glt.workspace-layout.v1')" : `setItem('glt.workspace-layout.v1',${JSON.stringify(previousLayout)})`}`);
 clearTimeout(timeout);ws.close();
}
