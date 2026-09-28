import { Profiler, useState } from "react";
import { createRoot } from "react-dom/client";
import PianoRollEditor from "../../src/PianoRollEditor";
import AnalysisView from "../../src/AnalysisView";
import { playbackStore } from "../../src/playbackRuntime";
import type { PerformanceDocument, AnalysisManifest } from "../../src/types";
import "../../src/styles.css";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const checks: string[] = [];
function assert(condition: unknown, label: string) { if (!condition) throw new Error(label); checks.push(label); }
const doc: PerformanceDocument = {
  format_version: 1, time_unit: "us", duration_us: 600_000_000,
  revision: { id: "base", parent_id: null, source: "transcribe" },
  source: { type: "audio", offset_us: 0 }, mapping: { profile: "default", transpose_semitones: 0 },
  tempo_map: [], beat_grid: [],
  notes: Array.from({ length: 5000 }, (_, index) => ({
    id: `n${index}`, start_us: index * 100_000, end_us: index * 100_000 + 80_000,
    key: "A", pitch: 60, velocity: 100, confidence: 0.9, source_stem: "other",
    candidate_id: null, original_pitch: 60, pitch_center: 60, pitch_bend_class: "stable", pitch_bends: [],
  })),
};
const manifest: AnalysisManifest = { format_version: 1, cache_key: "test", decode: { frames: 100, duration_us: doc.duration_us, sample_rate: 44100, channels: 2 }, waveform: { base_bucket_count: 2, levels: [] } };
let editorCommits = 0;
let visible = false;
const visibility = (value: boolean) => { visible = value; };
const noop = () => undefined;
let latest = doc;
function Fixture() {
  const [document, setDocument] = useState(doc);
  return <div style={{ width: "100%" }}>
    <input aria-label="outside editor" />
    <Profiler id="editor" onRender={() => editorCommits++}>
      <PianoRollEditor document={document} candidates={[]} applying={false} onDocumentChange={(next) => { latest = next; setDocument(next); }} onApply={noop} />
    </Profiler>
    <AnalysisView manifest={manifest} waveform={{ samples_per_bucket: 1, bucket_count: 2, data_base64: "AAD/fwAA/38=" }} spectrogram={null}
      selectionStartUs={null} selectionEndUs={null} onSeek={noop} onSelectionChange={noop} onAnalyzeSelection={noop} onTranscribeSelection={noop}
      loopEnabled={false} onToggleLoop={noop} onVisibilityChange={visibility} />
  </div>;
}
const root = createRoot(document.getElementById("root")!);
root.render(<Fixture />);

async function run() {
  await sleep(400);
  const editor = document.querySelector<HTMLElement>(".piano-roll")!;
  editor.scrollIntoView();
  await sleep(100);
  const before = editorCommits;
  let staticDraws = 0;
  const draw = CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.clearRect = function (...args) {
    if (["waveform-canvas", "piano-overlay", "velocity-lane"].includes((this.canvas as HTMLCanvasElement).className)) staticDraws++;
    return draw.apply(this, args);
  };
  for (let tick = 0; tick < 40; tick++) {
    playbackStore.setStatus({ position_us: tick * 50_000, available: true, paused: false });
    await sleep(50);
  }
  assert(editorCommits === before, "40 playback samples cause zero editor React commits");
  assert(staticDraws === 0, "playback does not redraw static canvas layers");
  const playbackMetrics = { staticDraws, editorCommits: editorCommits - before };
  const pointer = document.querySelector<HTMLElement>(".piano-playhead-track")!;
  assert(pointer.style.transform !== "translateX(0%)", "animation-frame playhead follows the external clock");
  playbackStore.setStatus({ position_us: 2_000_000, available: true, paused: true });
  await sleep(50);
  const paused = pointer.style.transform;
  await sleep(100);
  assert(pointer.style.transform === paused, "paused playhead remains stationary");
  const canvas = document.querySelector<HTMLCanvasElement>(".piano-webgl")!;
  const oldWidth = canvas.width;
  document.getElementById("root")!.style.width = "750px";
  await sleep(150);
  assert(canvas.width !== oldWidth && canvas.width === Math.round(canvas.clientWidth * devicePixelRatio), "resize updates WebGL backing size");
  editor.focus();
  editor.dispatchEvent(new KeyboardEvent("keydown", { key: "a", ctrlKey: true, bubbles: true }));
  await sleep(50);
  assert(editor.textContent?.includes("5000 个音符"), "focused editor handles select-all");
  const outside = document.querySelector<HTMLInputElement>("input[aria-label='outside editor']")!;
  outside.focus(); outside.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));
  await sleep(50);
  assert(latest.notes.length === 5000, "outside input does not delete selected notes");
  editor.focus(); editor.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));
  await sleep(50); assert(latest.notes.length === 0, "focused delete edits notes");
  editor.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }));
  await sleep(50); assert(latest.notes.length === 5000, "focused undo restores notes");
  const analysis = document.querySelector<HTMLElement>(".analysis-view")!;
  analysis.scrollIntoView(); await sleep(150);
  assert(visible, "visible analysis enables spectrum reads");
  analysis.style.display = "none"; await sleep(150);
  assert(!visible, "hidden analysis disables spectrum reads");
  CanvasRenderingContext2D.prototype.clearRect = draw;
  root.unmount();
  (window as unknown as { regression: unknown }).regression = { ok: true, checks, playbackMetrics };
}
void run().catch((error) => { (window as unknown as { regression: unknown }).regression = { ok: false, error: String(error), checks }; });
