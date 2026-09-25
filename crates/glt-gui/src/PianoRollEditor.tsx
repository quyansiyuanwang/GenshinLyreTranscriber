import { useEffect, useMemo, useRef, useState } from "react";

import {
  KEYBOARD_ORDER,
  addNote,
  commitHistory,
  deleteNotes,
  emptyHistory,
  keyForPitch,
  mergeNotes,
  moveNotes,
  pitchName,
  quantizeNotes,
  redoHistory,
  resizeNotes,
  setVelocities,
  splitNote,
  undoHistory,
  withNotes,
} from "./performanceEditor";
import type { CandidateNote, PerformanceDocument, PerformanceNote } from "./types";

const CANVAS_HEIGHT = 440;
const VELOCITY_HEIGHT = 92;
const MIN_VIEW_US = 500_000;

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  mode: "move" | "resize";
  baseNotes: PerformanceNote[];
  selected: Set<string>;
}

interface Props {
  document: PerformanceDocument;
  candidates: CandidateNote[];
  positionUs: number;
  applying: boolean;
  onDocumentChange: (document: PerformanceDocument) => void;
  onApply: () => void;
}

function formatTime(us: number): string {
  const seconds = us / 1_000_000;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(2).padStart(5, "0")}`;
}

function compileShader(
  gl: WebGL2RenderingContext,
  type: number,
  source: string,
): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("cannot create WebGL shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const message = gl.getShaderInfoLog(shader) ?? "WebGL shader compilation failed";
    gl.deleteShader(shader);
    throw new Error(message);
  }
  return shader;
}

function createProgram(gl: WebGL2RenderingContext): WebGLProgram {
  const vertex = compileShader(
    gl,
    gl.VERTEX_SHADER,
    `#version 300 es
    in vec2 a_position;
    in vec3 a_color;
    uniform vec2 u_resolution;
    out vec3 v_color;
    void main() {
      vec2 clip = vec2(
        (a_position.x / u_resolution.x) * 2.0 - 1.0,
        1.0 - (a_position.y / u_resolution.y) * 2.0
      );
      gl_Position = vec4(clip, 0.0, 1.0);
      v_color = a_color;
    }`,
  );
  const fragment = compileShader(
    gl,
    gl.FRAGMENT_SHADER,
    `#version 300 es
    precision mediump float;
    in vec3 v_color;
    out vec4 out_color;
    void main() { out_color = vec4(v_color, 1.0); }`,
  );
  const program = gl.createProgram();
  if (!program) throw new Error("cannot create WebGL program");
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const message = gl.getProgramInfoLog(program) ?? "WebGL program link failed";
    gl.deleteProgram(program);
    throw new Error(message);
  }
  return program;
}

function rectangle(
  data: number[],
  x: number,
  y: number,
  width: number,
  height: number,
  color: [number, number, number],
): void {
  const left = x;
  const right = x + Math.max(1, width);
  const top = y;
  const bottom = y + Math.max(1, height);
  for (const [px, py] of [
    [left, top],
    [right, top],
    [left, bottom],
    [left, bottom],
    [right, top],
    [right, bottom],
  ]) {
    data.push(px, py, color[0], color[1], color[2]);
  }
}

export default function PianoRollEditor({
  document,
  candidates,
  positionUs,
  applying,
  onDocumentChange,
  onApply,
}: Props) {
  const [localDocument, setLocalDocument] = useState(document);
  const [history, setHistory] = useState(emptyHistory);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tool, setTool] = useState<"select" | "add">("select");
  const [viewStartUs, setViewStartUs] = useState(0);
  const [viewDurationUs, setViewDurationUs] = useState(
    Math.max(MIN_VIEW_US, Math.min(document.duration_us, 30_000_000)),
  );
  const [dragPreview, setDragPreview] = useState<{ deltaUs: number; deltaPitch: number }>({
    deltaUs: 0,
    deltaPitch: 0,
  });
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const velocityRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<DragState | null>(null);

  const visibleDocument = useMemo(() => {
    if (!dragPreview.deltaUs && !dragPreview.deltaPitch) return localDocument;
    return withNotes(
      localDocument,
      moveNotes(
        localDocument.notes,
        selected,
        dragPreview.deltaUs,
        dragPreview.deltaPitch,
        localDocument.duration_us,
      ),
    );
  }, [dragPreview, localDocument, selected]);

  function commit(notes: PerformanceNote[], nextSelected?: Set<string>) {
    const next = withNotes(localDocument, notes);
    setHistory((current) => commitHistory(current, localDocument.notes));
    setLocalDocument(next);
    onDocumentChange(next);
    if (nextSelected) setSelected(nextSelected);
  }

  function undo() {
    const result = undoHistory(history, localDocument.notes);
    if (!result) return;
    const next = withNotes(localDocument, result.notes);
    setHistory(result.history);
    setLocalDocument(next);
    onDocumentChange(next);
  }

  function redo() {
    const result = redoHistory(history, localDocument.notes);
    if (!result) return;
    const next = withNotes(localDocument, result.notes);
    setHistory(result.history);
    setLocalDocument(next);
    onDocumentChange(next);
  }

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        event.shiftKey ? redo() : undo();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") {
        event.preventDefault();
        redo();
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        commit(deleteNotes(localDocument.notes, selected));
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl2");
    if (!gl) return;
    let program: WebGLProgram;
    try {
      program = createProgram(gl);
    } catch {
      return;
    }
    const count = 21;
    const laneHeight = (CANVAS_HEIGHT - 24) / count;
    const width = canvas.clientWidth;
    const height = CANVAS_HEIGHT;
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      gl.viewport(0, 0, canvas.width, canvas.height);
    }
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0.09, 0.11, 0.125, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const vertices: number[] = [];
    for (let lane = 0; lane < count; lane += 1) {
      const y = 12 + lane * laneHeight;
      rectangle(vertices, 0, y, width, laneHeight - 1, lane % 2 ? [0.105, 0.13, 0.145] : [0.095, 0.117, 0.132]);
    }
    const divisions = 10;
    for (let division = 0; division <= divisions; division += 1) {
      rectangle(vertices, (division / divisions) * width, 0, 1, height, [0.19, 0.23, 0.255]);
    }
    for (const note of visibleDocument.notes) {
      const lane = KEYBOARD_ORDER.indexOf(note.key);
      if (lane < 0) continue;
      const x =
        ((note.start_us - viewStartUs) / viewDurationUs) * width;
      const noteWidth = ((note.end_us - note.start_us) / viewDurationUs) * width;
      if (x + noteWidth < 0 || x > width) continue;
      const isSelected = selected.has(note.id);
      const color: [number, number, number] = isSelected
        ? [1.0, 0.77, 0.25]
        : note.source_stem === "edit"
          ? [0.52, 0.72, 0.86]
          : [0.37, 0.58, 0.72];
      rectangle(
        vertices,
        Math.max(-2, x),
        15 + lane * laneHeight,
        noteWidth,
        laneHeight - 5,
        color,
      );
    }
    rectangle(
      vertices,
      ((positionUs - viewStartUs) / viewDurationUs) * width,
      0,
      2,
      height,
      [0.95, 0.35, 0.27],
    );
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
    const stride = 5 * Float32Array.BYTES_PER_ELEMENT;
    const position = gl.getAttribLocation(program, "a_position");
    const color = gl.getAttribLocation(program, "a_color");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(color);
    gl.vertexAttribPointer(color, 3, gl.FLOAT, false, stride, 2 * Float32Array.BYTES_PER_ELEMENT);
    gl.useProgram(program);
    gl.uniform2f(gl.getUniformLocation(program, "u_resolution"), width, height);
    gl.drawArrays(gl.TRIANGLES, 0, vertices.length / 5);
    gl.deleteBuffer(buffer);
    gl.deleteProgram(program);
  }, [positionUs, selected, viewDurationUs, viewStartUs, visibleDocument]);

  useEffect(() => {
    const overlay = overlayRef.current;
    if (!overlay) return;
    const ratio = window.devicePixelRatio || 1;
    const width = overlay.clientWidth;
    const height = CANVAS_HEIGHT;
    overlay.width = Math.round(width * ratio);
    overlay.height = Math.round(height * ratio);
    const context = overlay.getContext("2d");
    if (!context) return;
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);
    const laneHeight = (height - 24) / 21;
    context.fillStyle = "rgba(118, 169, 207, 0.20)";
    context.strokeStyle = "rgba(118, 169, 207, 0.65)";
    for (const candidate of candidates) {
      const lane = KEYBOARD_ORDER.indexOf(keyForPitch(candidate.pitch));
      const x = ((candidate.start_us - viewStartUs) / viewDurationUs) * width;
      const noteWidth = ((candidate.end_us - candidate.start_us) / viewDurationUs) * width;
      if (lane < 0 || x + noteWidth < 0 || x > width) continue;
      context.fillRect(x, 15 + lane * laneHeight, Math.max(1, noteWidth), laneHeight - 5);
      context.strokeRect(x, 15 + lane * laneHeight, Math.max(1, noteWidth), laneHeight - 5);
    }
    for (const note of visibleDocument.notes) {
      if (!selected.has(note.id)) continue;
      const lane = KEYBOARD_ORDER.indexOf(note.key);
      const x = ((note.start_us - viewStartUs) / viewDurationUs) * width;
      const noteWidth = ((note.end_us - note.start_us) / viewDurationUs) * width;
      context.strokeStyle = "#ffd35d";
      context.lineWidth = 2;
      context.strokeRect(x, 15 + lane * laneHeight, Math.max(2, noteWidth), laneHeight - 5);
      if (note.pitch_bends.length >= 2) {
        context.beginPath();
        context.strokeStyle = "#f39a32";
        context.lineWidth = 1.5;
        note.pitch_bends.forEach((point, index) => {
          const bendX = ((point.at_us - viewStartUs) / viewDurationUs) * width;
          const pitch = note.pitch + point.cents / 100;
          const bendY =
            15 + (KEYBOARD_ORDER.indexOf(keyForPitch(pitch)) + 0.5) * laneHeight;
          if (index === 0) context.moveTo(bendX, bendY);
          else context.lineTo(bendX, bendY);
        });
        context.stroke();
      }
    }
  }, [candidates, selected, viewDurationUs, viewStartUs, visibleDocument]);

  useEffect(() => {
    const canvas = velocityRef.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(VELOCITY_HEIGHT * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, VELOCITY_HEIGHT);
    context.fillStyle = "#191e21";
    context.fillRect(0, 0, width, VELOCITY_HEIGHT);
    context.strokeStyle = "#364044";
    for (let value = 32; value <= 127; value += 32) {
      const y = VELOCITY_HEIGHT - (value / 127) * (VELOCITY_HEIGHT - 14);
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }
    for (const note of visibleDocument.notes) {
      const x = ((note.start_us - viewStartUs) / viewDurationUs) * width;
      const height = (note.velocity / 127) * (VELOCITY_HEIGHT - 14);
      context.fillStyle = selected.has(note.id) ? "#ffd35d" : "#6b9fc7";
      context.fillRect(x, VELOCITY_HEIGHT - height, 2, height);
    }
  }, [selected, viewDurationUs, viewStartUs, visibleDocument]);

  function point(event: React.PointerEvent<HTMLCanvasElement>): { x: number; y: number; timeUs: number; lane: number } {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const laneHeight = (CANVAS_HEIGHT - 24) / 21;
    const lane = Math.max(0, Math.min(20, Math.floor((y - 15) / laneHeight)));
    return {
      x,
      y,
      timeUs: viewStartUs + (x / Math.max(1, rect.width)) * viewDurationUs,
      lane,
    };
  }

  function hitTest(x: number, y: number): { note: PerformanceNote; resize: boolean } | null {
    const canvas = overlayRef.current;
    if (!canvas) return null;
    const width = canvas.clientWidth;
    const laneHeight = (CANVAS_HEIGHT - 24) / 21;
    for (const note of [...visibleDocument.notes].reverse()) {
      const lane = KEYBOARD_ORDER.indexOf(note.key);
      const left = ((note.start_us - viewStartUs) / viewDurationUs) * width;
      const right = ((note.end_us - viewStartUs) / viewDurationUs) * width;
      const top = 15 + lane * laneHeight;
      if (x >= left && x <= right && y >= top && y <= top + laneHeight - 5) {
        return { note, resize: right - x <= 8 };
      }
    }
    return null;
  }

  function onPointerDown(event: React.PointerEvent<HTMLCanvasElement>) {
    const selectedPoint = point(event);
    if (tool === "add") {
      const key = KEYBOARD_ORDER[selectedPoint.lane];
      const result = addNote(localDocument.notes, selectedPoint.timeUs, key, localDocument.duration_us);
      commit(result.notes, new Set([result.id]));
      return;
    }
    const hit = hitTest(selectedPoint.x, selectedPoint.y);
    if (!hit) {
      if (!event.shiftKey) setSelected(new Set());
      return;
    }
    const nextSelected = new Set(event.shiftKey ? selected : []);
    if (nextSelected.has(hit.note.id) && event.shiftKey) nextSelected.delete(hit.note.id);
    else nextSelected.add(hit.note.id);
    setSelected(nextSelected);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: selectedPoint.x,
      startY: selectedPoint.y,
      mode: hit.resize ? "resize" : "move",
      baseNotes: localDocument.notes,
      selected: nextSelected,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: React.PointerEvent<HTMLCanvasElement>) {
    const drag = dragRef.current;
    const active = hitTest(point(event).x, point(event).y);
    event.currentTarget.style.cursor = active?.resize ? "ew-resize" : active ? "grab" : "crosshair";
    if (!drag || drag.pointerId !== event.pointerId) return;
    const canvas = overlayRef.current;
    if (!canvas) return;
    const current = point(event);
    const rawDeltaUs = ((current.x - drag.startX) / Math.max(1, canvas.clientWidth)) * viewDurationUs;
    const deltaUs = Math.round(rawDeltaUs / 10_000) * 10_000;
    const deltaPitch = -Math.round((current.y - drag.startY) / ((CANVAS_HEIGHT - 24) / 21));
    setDragPreview({ deltaUs, deltaPitch: drag.mode === "resize" ? 0 : deltaPitch });
  }

  function onPointerUp(event: React.PointerEvent<HTMLCanvasElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const canvas = overlayRef.current;
    if (!canvas) return;
    const current = point(event);
    const deltaUs = Math.round(
      ((((current.x - drag.startX) / Math.max(1, canvas.clientWidth)) * viewDurationUs) / 10_000),
    ) * 10_000;
    const deltaPitch = drag.mode === "resize"
      ? 0
      : -Math.round((current.y - drag.startY) / ((CANVAS_HEIGHT - 24) / 21));
    const notes = drag.mode === "resize"
      ? resizeNotes(drag.baseNotes, drag.selected, deltaUs, localDocument.duration_us)
      : moveNotes(drag.baseNotes, drag.selected, deltaUs, deltaPitch, localDocument.duration_us);
    dragRef.current = null;
    setDragPreview({ deltaUs: 0, deltaPitch: 0 });
    if (deltaUs || deltaPitch) commit(notes);
  }

  function changeVelocity(event: React.PointerEvent<HTMLCanvasElement>) {
    if (!selected.size) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, 1 - (event.clientY - rect.top) / rect.height));
    commit(setVelocities(localDocument.notes, selected, ratio * 127));
  }

  const selectedNote = localDocument.notes.find((note) => selected.has(note.id));
  const selectionLabel = selected.size
    ? `${selected.size} 个音符${selectedNote ? ` · ${selectedNote.key}/${pitchName(selectedNote.pitch)} · v${selectedNote.velocity}` : ""}`
    : "未选择音符";

  return (
    <div className="piano-roll">
      <div className="piano-toolbar">
        <div className="button-row">
          <button className={tool === "select" ? "primary-button" : "ghost-button"} onClick={() => setTool("select")}>选择</button>
          <button className={tool === "add" ? "primary-button" : "ghost-button"} onClick={() => setTool("add")}>添加</button>
          <button onClick={undo} disabled={!history.past.length}>撤销</button>
          <button onClick={redo} disabled={!history.future.length}>重做</button>
          <button onClick={() => setViewDurationUs((value) => Math.max(MIN_VIEW_US, value * 0.75))}>放大</button>
          <button onClick={() => setViewDurationUs((value) => Math.min(localDocument.duration_us, value * 1.35))}>缩小</button>
        </div>
        <span>{selectionLabel}</span>
      </div>
      <div className="piano-canvas-wrap">
        <canvas ref={canvasRef} className="piano-webgl" />
        <canvas
          ref={overlayRef}
          className="piano-overlay"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            dragRef.current = null;
            setDragPreview({ deltaUs: 0, deltaPitch: 0 });
          }}
        />
      </div>
      <div className="piano-lanes">
        <span>{formatTime(viewStartUs)}</span>
        <input
          type="range"
          min={0}
          max={Math.max(0, localDocument.duration_us - viewDurationUs)}
          value={Math.min(viewStartUs, Math.max(0, localDocument.duration_us - viewDurationUs))}
          onChange={(event) => setViewStartUs(Number(event.target.value))}
        />
        <span>{formatTime(Math.min(localDocument.duration_us, viewStartUs + viewDurationUs))}</span>
      </div>
      <div className="velocity-title">
        <span>Velocity</span>
        <small>在轨道中拖动可设置所选力度</small>
      </div>
      <canvas ref={velocityRef} className="velocity-lane" onPointerDown={changeVelocity} onPointerMove={(event) => event.buttons === 1 && changeVelocity(event)} />
      <div className="button-row edit-actions">
        <button
          onClick={() => {
            if (!selectedNote) return;
            commit(splitNote(localDocument.notes, selectedNote.id, positionUs));
          }}
          disabled={!selectedNote}
        >
          在播放位置拆分
        </button>
        <button onClick={() => commit(mergeNotes(localDocument.notes, selected))} disabled={selected.size < 2}>合并</button>
        <button onClick={() => commit(quantizeNotes(localDocument.notes, selected, 10_000, localDocument.duration_us))} disabled={!selected.size}>量化 10ms</button>
        <button onClick={() => commit(quantizeNotes(localDocument.notes, selected, 50_000, localDocument.duration_us))} disabled={!selected.size}>量化 50ms</button>
        <button className="danger-button" onClick={() => commit(deleteNotes(localDocument.notes, selected))} disabled={!selected.size}>删除</button>
        <button className="primary-button" onClick={onApply} disabled={applying || !localDocument.notes.length}>应用并导出 edit-NN</button>
      </div>
    </div>
  );
}
