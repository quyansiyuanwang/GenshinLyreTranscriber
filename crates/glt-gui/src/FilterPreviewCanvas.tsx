import { useEffect, useMemo, useRef, useState } from "react";

import { liveFilterStats } from "./filterPreview";
import {
  FILTER_METRICS,
  filterMetricDefinition,
  metricRangeAround,
  metricRuleRange,
  quantizeMetric,
  suggestedMetricRange,
  type FilterMetric,
  type FilterMetricBound,
} from "./filterMetrics";
import type { CandidateNote, DraftFilterRule } from "./types";

interface Props {
  notes: CandidateNote[];
  rules: DraftFilterRule[];
  activeRuleIndex: number;
  onActiveRuleChange: (ruleIndex: number) => void;
  onRangeChange: (
    ruleIndex: number,
    metric: FilterMetric,
    lower: number,
    upper: number,
  ) => void;
  onClearRange: (ruleIndex: number, metric: FilterMetric) => void;
}

interface DragLine {
  pointerId: number;
  ruleIndex: number;
  metric: FilterMetric;
  bound: FilterMetricBound;
}

interface RangeDraft {
  pointerId: number;
  metric: FilterMetric;
  anchor: number;
  current: number;
}

const RULE_COLORS = ["#f3a347", "#5bc0de", "#86c66f", "#d783d8"] as const;

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

function fillRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
): void {
  context.beginPath();
  context.rect(x, y, width, height);
  context.fill();
}

export default function FilterPreviewCanvas({
  notes,
  rules,
  activeRuleIndex,
  onActiveRuleChange,
  onRangeChange,
  onClearRange,
}: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<DragLine | null>(null);
  const draftRef = useRef<RangeDraft | null>(null);
  const [metric, setMetric] = useState<FilterMetric>("pitch");
  const [createMode, setCreateMode] = useState(false);
  const [rangeDraft, setRangeDraft] = useState<RangeDraft | null>(null);
  const stats = useMemo(() => liveFilterStats(notes, rules), [notes, rules]);
  const definition = filterMetricDefinition(metric);

  const values = useMemo(
    () =>
      notes
        .map((note) => definition.value(note))
        .filter((value): value is number => value !== null && Number.isFinite(value)),
    [definition, notes],
  );
  const durationUs = Math.max(1, notes.reduce((maximum, note) => Math.max(maximum, note.end_us), 0));
  const domainMinimum = definition.minimum;
  const domainMaximum = useMemo(() => {
    if (metric !== "duration") return definition.maximum;
    const observedMaximum = Math.max(1000, ...values);
    return Math.max(1000, Math.min(definition.maximum, observedMaximum * 1.18));
  }, [definition.maximum, metric, values]);

  const activeRange = rules[activeRuleIndex]
    ? metricRuleRange(rules[activeRuleIndex], metric)
    : { lower: null, upper: null };
  const metricLines = useMemo(
    () =>
      rules.flatMap((rule, ruleIndex) => {
        if (!rule.enabled) return [];
        const range = metricRuleRange(rule, metric);
        return [
          ...(range.lower === null
            ? []
            : [{ ruleIndex, bound: "min" as const, value: range.lower }]),
          ...(range.upper === null
            ? []
            : [{ ruleIndex, bound: "max" as const, value: range.upper }]),
        ];
      }),
    [metric, rules],
  );

  function metricY(value: number, height: number): number {
    const ratio = (value - domainMinimum) / Math.max(0.0001, domainMaximum - domainMinimum);
    return height - Math.min(1.15, Math.max(-0.15, ratio)) * height;
  }

  function metricFromPointer(event: {
    clientY: number;
    currentTarget: HTMLCanvasElement;
  }): number {
    const bounds = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height));
    return quantizeMetric(metric, domainMaximum - ratio * (domainMaximum - domainMinimum));
  }

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const left = 44;
    const right = 10;
    const top = 8;
    const bottom = 22;
    const plotWidth = Math.max(1, width - left - right);
    const plotHeight = Math.max(1, height - top - bottom);
    const plotY = (value: number) => top + metricY(value, plotHeight);

    context.fillStyle = "#171c1f";
    context.fillRect(0, 0, width, height);
    context.fillStyle = "#131719";
    context.fillRect(left, top, plotWidth, plotHeight);

    context.strokeStyle = "#30393d";
    context.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) {
      const y = Math.round(top + (plotHeight * index) / 4) + 0.5;
      context.beginPath();
      context.moveTo(left, y);
      context.lineTo(left + plotWidth, y);
      context.stroke();
      const value = domainMaximum - ((domainMaximum - domainMinimum) * index) / 4;
      context.fillStyle = "#667174";
      context.font = "8px Cascadia Mono, Consolas, monospace";
      context.textAlign = "right";
      context.fillText(metric === "confidence" ? value.toFixed(2) : value.toFixed(0), left - 5, y + 3);
    }
    for (let index = 0; index <= 8; index += 1) {
      const x = Math.round(left + (plotWidth * index) / 8) + 0.5;
      context.strokeStyle = index % 4 === 0 ? "#374246" : "#283034";
      context.beginPath();
      context.moveTo(x, top);
      context.lineTo(x, top + plotHeight);
      context.stroke();
    }
    context.fillStyle = "#667174";
    context.font = "8px Cascadia Mono, Consolas, monospace";
    context.textAlign = "left";
    context.fillText("00:00", left, height - 6);
    context.textAlign = "right";
    context.fillText(`${(durationUs / 1_000_000).toFixed(1)}s`, left + plotWidth, height - 6);

    rules.forEach((rule, ruleIndex) => {
      if (!rule.enabled) return;
      const range = metricRuleRange(rule, metric);
      if (range.lower === null && range.upper === null) return;
      const lower = range.lower ?? domainMinimum;
      const upper = range.upper ?? domainMaximum;
      const upperY = plotY(Math.max(lower, upper));
      const lowerY = plotY(Math.min(lower, upper));
      context.globalAlpha = ruleIndex === activeRuleIndex ? 0.13 : 0.055;
      context.fillStyle = RULE_COLORS[ruleIndex % RULE_COLORS.length];
      fillRect(context, left, upperY, plotWidth, Math.max(1, lowerY - upperY));
    });
    context.globalAlpha = 1;

    notes.forEach((note, index) => {
      const value = definition.value(note);
      if (value === null) return;
      const x = left + (note.start_us / durationUs) * plotWidth;
      const noteWidth = Math.max(1.5, ((note.end_us - note.start_us) / durationUs) * plotWidth);
      const y = plotY(value);
      const kept = stats.matches[index];
      context.globalAlpha = kept ? 0.84 : 0.3;
      context.fillStyle = kept ? "#8fc46c" : "#d66d68";
      context.fillRect(x, y - 2.5, noteWidth, Math.max(3, (note.velocity / 127) * 7));
    });
    context.globalAlpha = 1;

    metricLines.forEach((line) => {
      const y = Math.round(plotY(line.value)) + 0.5;
      const active = line.ruleIndex === activeRuleIndex;
      context.save();
      context.strokeStyle = RULE_COLORS[line.ruleIndex % RULE_COLORS.length];
      context.globalAlpha = active ? 1 : 0.45;
      context.setLineDash(active ? [] : [5, 4]);
      context.lineWidth = active ? 1.5 : 1;
      context.beginPath();
      context.moveTo(left, y);
      context.lineTo(left + plotWidth, y);
      context.stroke();
      if (active) {
        context.setLineDash([]);
        context.fillStyle = RULE_COLORS[line.ruleIndex % RULE_COLORS.length];
        context.fillRect(left, y - 3, 7, 7);
        context.fillRect(left + plotWidth - 7, y - 3, 7, 7);
        context.font = "8px Cascadia Mono, Consolas, monospace";
        context.textAlign = "left";
        context.fillText(
          `${line.bound === "min" ? "MIN" : "MAX"} ${line.value.toFixed(metric === "confidence" ? 2 : 0)}`,
          left + 10,
          y - 4,
        );
      }
      context.restore();
    });

    if (rangeDraft && rangeDraft.metric === metric) {
      const startY = plotY(rangeDraft.anchor);
      const currentY = plotY(rangeDraft.current);
      const topY = Math.min(startY, currentY);
      const bottomY = Math.max(startY, currentY);
      context.globalAlpha = 0.18;
      context.fillStyle = "#f3a347";
      fillRect(context, left, topY, plotWidth, Math.max(1, bottomY - topY));
      context.globalAlpha = 1;
      context.strokeStyle = "#ffd59e";
      context.setLineDash([4, 3]);
      context.beginPath();
      context.moveTo(left, startY + 0.5);
      context.lineTo(left + plotWidth, startY + 0.5);
      context.moveTo(left, currentY + 0.5);
      context.lineTo(left + plotWidth, currentY + 0.5);
      context.stroke();
      context.setLineDash([]);
    } else if (activeRange.lower === null && activeRange.upper === null && notes.length > 0) {
      const suggested = suggestedMetricRange(notes, metric);
      const y1 = plotY(suggested.lower);
      const y2 = plotY(suggested.upper);
      context.strokeStyle = "#526064";
      context.setLineDash([2, 4]);
      context.beginPath();
      context.moveTo(left, y1 + 0.5);
      context.lineTo(left + plotWidth, y1 + 0.5);
      context.moveTo(left, y2 + 0.5);
      context.lineTo(left + plotWidth, y2 + 0.5);
      context.stroke();
      context.setLineDash([]);
    }
  }, [
    activeRange.lower,
    activeRange.upper,
    activeRuleIndex,
    definition,
    domainMaximum,
    domainMinimum,
    durationUs,
    metric,
    metricLines,
    notes,
    rangeDraft,
    rules,
    stats.matches,
  ]);

  function finishPointer(event: React.PointerEvent<HTMLCanvasElement>) {
    const draft = draftRef.current;
    if (draft) {
      const lower = Math.min(draft.anchor, draft.current);
      const upper = Math.max(draft.anchor, draft.current);
      const range =
        Math.abs(upper - lower) < definition.step
          ? metricRangeAround(notes, metric, draft.anchor)
          : { lower, upper };
      onRangeChange(activeRuleIndex, metric, range.lower, range.upper);
    }
    dragRef.current = null;
    draftRef.current = null;
    setRangeDraft(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  return (
    <div className="filter-preview-workbench">
      <div className="filter-preview-toolbar">
        <div className="filter-metric-tabs" role="tablist" aria-label="筛选指标">
          {FILTER_METRICS.map((candidate) => (
            <button
              type="button"
              role="tab"
              aria-selected={metric === candidate.id}
              className={metric === candidate.id ? "active" : ""}
              key={candidate.id}
              onClick={() => {
                setMetric(candidate.id);
                setCreateMode(false);
              }}
            >
              {candidate.shortLabel}
            </button>
          ))}
        </div>
        <div className="filter-canvas-actions">
          <span style={{ color: RULE_COLORS[activeRuleIndex % RULE_COLORS.length] }}>
            RULE {activeRuleIndex + 1}
          </span>
          <button
            type="button"
            className={createMode ? "active" : ""}
            aria-pressed={createMode}
            onClick={() => setCreateMode((current) => !current)}
          >
            {createMode ? "拖动创建中" : "+ 画横向范围"}
          </button>
          <button
            type="button"
            onClick={() => {
              const range = suggestedMetricRange(notes, metric);
              onRangeChange(activeRuleIndex, metric, range.lower, range.upper);
              setCreateMode(false);
            }}
          >
            一键范围
          </button>
          <button
            type="button"
            disabled={activeRange.lower === null && activeRange.upper === null}
            onClick={() => onClearRange(activeRuleIndex, metric)}
          >
            清除本指标
          </button>
        </div>
      </div>
      <canvas
        ref={ref}
        className={`filter-preview-canvas ${createMode ? "create-mode" : ""}`}
        aria-label={`候选音符 ${definition.label} 分布图；拖动横线调整，双击创建范围`}
        onDoubleClick={(event) => {
          const range = metricRangeAround(notes, metric, metricFromPointer(event));
          onRangeChange(activeRuleIndex, metric, range.lower, range.upper);
          setCreateMode(false);
        }}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          const bounds = event.currentTarget.getBoundingClientRect();
          const pointerY = event.clientY - bounds.top;
          if (createMode) {
            const draft: RangeDraft = {
              pointerId: event.pointerId,
              metric,
              anchor: metricFromPointer(event),
              current: metricFromPointer(event),
            };
            draftRef.current = draft;
            setRangeDraft(draft);
            event.currentTarget.setPointerCapture(event.pointerId);
            return;
          }
          const candidates = metricLines
            .map((line) => ({
              ...line,
              y: 8 + metricY(line.value, Math.max(1, bounds.height - 30)),
            }))
            .sort((left, right) => Math.abs(left.y - pointerY) - Math.abs(right.y - pointerY));
          const nearest = candidates[0];
          if (!nearest || Math.abs(nearest.y - pointerY) > 10) return;
          onActiveRuleChange(nearest.ruleIndex);
          dragRef.current = {
            pointerId: event.pointerId,
            ruleIndex: nearest.ruleIndex,
            metric,
            bound: nearest.bound,
          };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          const draft = draftRef.current;
          if (draft && draft.pointerId === event.pointerId) {
            const next = { ...draft, current: metricFromPointer(event) };
            draftRef.current = next;
            setRangeDraft(next);
            return;
          }
          const drag = dragRef.current;
          if (!drag || drag.pointerId !== event.pointerId) return;
          const range = metricRuleRange(rules[drag.ruleIndex], drag.metric);
          const value = metricFromPointer(event);
          if (drag.bound === "min") {
            onRangeChange(drag.ruleIndex, drag.metric, value, range.upper ?? domainMaximum);
          } else {
            onRangeChange(drag.ruleIndex, drag.metric, range.lower ?? domainMinimum, value);
          }
        }}
        onPointerUp={finishPointer}
        onPointerCancel={finishPointer}
      />
      <div className="filter-preview-footer">
        <div className="filter-preview-legend">
          <span><i className="kept" />保留</span>
          <span><i className="removed" />删除</span>
          <span><i className="threshold" />当前规则横线</span>
        </div>
        <strong>
          {createMode
            ? "在图上拖动：向上/向下画出上下限"
            : activeRange.lower === null && activeRange.upper === null
              ? "双击图上任意位置即可创建范围"
              : `${definition.label} ${activeRange.lower ?? domainMinimum} - ${activeRange.upper ?? domainMaximum}${definition.unit}`}
        </strong>
      </div>
    </div>
  );
}
