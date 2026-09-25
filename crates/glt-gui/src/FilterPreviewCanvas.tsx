import { useEffect, useMemo, useRef } from "react";

import { activePitchLines, liveFilterStats } from "./filterPreview";
import type { CandidateNote, DraftFilterRule } from "./types";

type PitchBound = "min" | "max";

interface Props {
  notes: CandidateNote[];
  rules: DraftFilterRule[];
  onPitchLineChange: (ruleIndex: number, bound: PitchBound, pitch: number) => void;
}

interface DragLine {
  pointerId: number;
  ruleIndex: number;
  bound: PitchBound;
}

const MIN_PITCH = 0;
const MAX_PITCH = 127;

function setupCanvas(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context?.setTransform(ratio, 0, 0, ratio, 0, 0);
  return context;
}

function clampPitch(value: number): number {
  return Math.min(MAX_PITCH, Math.max(MIN_PITCH, Math.round(value)));
}

function pitchY(pitch: number, height: number): number {
  return height - ((pitch - MIN_PITCH) / (MAX_PITCH - MIN_PITCH)) * height;
}

function pitchFromPointer(event: React.PointerEvent<HTMLCanvasElement>): number {
  const bounds = event.currentTarget.getBoundingClientRect();
  const ratio = (event.clientY - bounds.top) / Math.max(1, bounds.height);
  return clampPitch((1 - ratio) * (MAX_PITCH - MIN_PITCH) + MIN_PITCH);
}

export default function FilterPreviewCanvas({ notes, rules, onPitchLineChange }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<DragLine | null>(null);
  const stats = useMemo(() => liveFilterStats(notes, rules), [notes, rules]);
  const pitchLines = useMemo(() => activePitchLines(rules), [rules]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const durationUs = Math.max(1, notes.reduce((maximum, note) => Math.max(maximum, note.end_us), 0));

    context.fillStyle = "#171c1f";
    context.fillRect(0, 0, width, height);
    context.strokeStyle = "#30393d";
    context.lineWidth = 1;
    for (let pitch = 12; pitch < 127; pitch += 12) {
      const y = Math.round(pitchY(pitch, height)) + 0.5;
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }

    for (const line of pitchLines) {
      context.save();
      context.strokeStyle = "#f3a347";
      context.globalAlpha = 0.92;
      context.setLineDash([5, 4]);
      context.lineWidth = 1;
      for (const pitch of [line.min, line.max]) {
        if (pitch === null) continue;
        const y = Math.round(pitchY(pitch, height)) + 0.5;
        context.beginPath();
        context.moveTo(0, y);
        context.lineTo(width, y);
        context.stroke();
        context.setLineDash([]);
        context.fillStyle = "#f3a347";
        context.fillRect(4, y - 3, 7, 7);
        context.fillRect(width - 11, y - 3, 7, 7);
        context.setLineDash([5, 4]);
      }
      context.restore();
    }

    notes.forEach((note, index) => {
      const x = (note.start_us / durationUs) * width;
      const noteWidth = Math.max(2, ((note.end_us - note.start_us) / durationUs) * width);
      const y = pitchY(note.pitch, height);
      const kept = stats.matches[index];
      context.globalAlpha = kept ? 0.86 : 0.34;
      context.fillStyle = kept ? "#8fc46c" : "#d66d68";
      context.fillRect(x, y - 2.5, noteWidth, Math.max(3, (note.velocity / 127) * 7));
    });
    context.globalAlpha = 1;
  }, [notes, pitchLines, stats.matches]);

  return (
    <div className="filter-preview-canvas-wrap">
      <canvas
        ref={ref}
        className="filter-preview-canvas"
        aria-label="候选音符筛选分布图；拖动橙色横线调整 pitch 上下限"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          const bounds = event.currentTarget.getBoundingClientRect();
          const y = event.clientY - bounds.top;
          const candidates = pitchLines.flatMap((line) => [
            ...(line.min === null ? [] : [{ ruleIndex: line.ruleIndex, bound: "min" as const, y: pitchY(line.min, bounds.height) }]),
            ...(line.max === null ? [] : [{ ruleIndex: line.ruleIndex, bound: "max" as const, y: pitchY(line.max, bounds.height) }]),
          ]);
          const nearest = candidates.sort((left, right) => Math.abs(left.y - y) - Math.abs(right.y - y))[0];
          if (!nearest || Math.abs(nearest.y - y) > 7) return;
          dragRef.current = { pointerId: event.pointerId, ruleIndex: nearest.ruleIndex, bound: nearest.bound };
          event.currentTarget.setPointerCapture(event.pointerId);
          onPitchLineChange(nearest.ruleIndex, nearest.bound, pitchFromPointer(event));
        }}
        onPointerMove={(event) => {
          const drag = dragRef.current;
          if (!drag || drag.pointerId !== event.pointerId) return;
          onPitchLineChange(drag.ruleIndex, drag.bound, pitchFromPointer(event));
        }}
        onPointerUp={(event) => {
          dragRef.current = null;
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
        onPointerCancel={(event) => {
          dragRef.current = null;
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
      />
      <div className="filter-preview-legend">
        <span><i className="kept" />当前阈值保留</span>
        <span><i className="removed" />当前阈值删除</span>
        <span><i className="threshold" />拖动橙色横线调整 pitch</span>
      </div>
    </div>
  );
}
