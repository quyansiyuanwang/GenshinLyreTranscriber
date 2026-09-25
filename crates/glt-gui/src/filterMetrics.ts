import type { CandidateNote, DraftFilterRule } from "./types";

export type NumericFilterKey = Exclude<keyof DraftFilterRule, "enabled">;
export type FilterMetric = "pitch" | "velocity" | "confidence" | "duration";
export type FilterMetricBound = "min" | "max";

export interface FilterMetricDefinition {
  id: FilterMetric;
  label: string;
  shortLabel: string;
  minimumKey: NumericFilterKey;
  maximumKey: NumericFilterKey;
  minimum: number;
  maximum: number;
  step: number;
  unit: string;
  value: (note: CandidateNote) => number | null;
}

function durationMs(note: CandidateNote): number {
  return Math.max(0, note.end_us - note.start_us) / 1000;
}

export const FILTER_METRICS: readonly FilterMetricDefinition[] = [
  {
    id: "pitch",
    label: "MIDI pitch / 音高",
    shortLabel: "PITCH",
    minimumKey: "pitchMin",
    maximumKey: "pitchMax",
    minimum: 0,
    maximum: 127,
    step: 1,
    unit: "",
    value: (note) => note.pitch,
  },
  {
    id: "velocity",
    label: "velocity / 力度",
    shortLabel: "VELOCITY",
    minimumKey: "velocityMin",
    maximumKey: "velocityMax",
    minimum: 1,
    maximum: 127,
    step: 1,
    unit: "",
    value: (note) => note.velocity,
  },
  {
    id: "confidence",
    label: "confidence / 置信度",
    shortLabel: "CONFIDENCE",
    minimumKey: "confidenceMin",
    maximumKey: "confidenceMax",
    minimum: 0,
    maximum: 1,
    step: 0.01,
    unit: "",
    value: (note) => note.confidence,
  },
  {
    id: "duration",
    label: "duration / 时值",
    shortLabel: "DURATION",
    minimumKey: "durationMin",
    maximumKey: "durationMax",
    minimum: 0,
    maximum: 10000,
    step: 10,
    unit: "ms",
    value: durationMs,
  },
] as const;

export function filterMetricDefinition(metric: FilterMetric): FilterMetricDefinition {
  return FILTER_METRICS.find((definition) => definition.id === metric) ?? FILTER_METRICS[0];
}

export function clampMetric(metric: FilterMetric, value: number): number {
  const definition = filterMetricDefinition(metric);
  return Math.min(definition.maximum, Math.max(definition.minimum, value));
}

export function quantizeMetric(metric: FilterMetric, value: number): number {
  const definition = filterMetricDefinition(metric);
  const clamped = clampMetric(metric, value);
  return Math.round(clamped / definition.step) * definition.step;
}

function quantile(values: number[], ratio: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.round((sorted.length - 1) * ratio)));
  return sorted[index];
}

export function suggestedMetricRange(
  notes: CandidateNote[],
  metric: FilterMetric,
): { lower: number; upper: number } {
  const definition = filterMetricDefinition(metric);
  const values = notes
    .map((note) => definition.value(note))
    .filter((value): value is number => value !== null && Number.isFinite(value));
  if (values.length === 0) {
    const span = definition.maximum - definition.minimum;
    return {
      lower: quantizeMetric(metric, definition.minimum + span * 0.2),
      upper: quantizeMetric(metric, definition.minimum + span * 0.8),
    };
  }
  let lower = quantile(values, 0.15);
  let upper = quantile(values, 0.85);
  const minimumSpan = definition.step * (metric === "pitch" ? 12 : 4);
  if (upper - lower < minimumSpan) {
    const center = (lower + upper) / 2;
    lower = center - minimumSpan / 2;
    upper = center + minimumSpan / 2;
  }
  return {
    lower: quantizeMetric(metric, lower),
    upper: quantizeMetric(metric, upper),
  };
}

export function metricRangeAround(
  notes: CandidateNote[],
  metric: FilterMetric,
  center: number,
): { lower: number; upper: number } {
  const definition = filterMetricDefinition(metric);
  const suggested = suggestedMetricRange(notes, metric);
  const halfSpan = Math.max(definition.step, Math.abs(suggested.upper - suggested.lower) / 2);
  return {
    lower: quantizeMetric(metric, center - halfSpan),
    upper: quantizeMetric(metric, center + halfSpan),
  };
}

export function metricRuleRange(
  rule: DraftFilterRule,
  metric: FilterMetric,
): { lower: number | null; upper: number | null } {
  const definition = filterMetricDefinition(metric);
  const parse = (value: string): number | null => {
    if (!value.trim()) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  return {
    lower: parse(rule[definition.minimumKey]),
    upper: parse(rule[definition.maximumKey]),
  };
}
