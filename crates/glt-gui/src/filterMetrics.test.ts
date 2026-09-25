import { describe, expect, it } from "vitest";

import {
  filterMetricDefinition,
  metricRangeAround,
  metricRuleRange,
  quantizeMetric,
  suggestedMetricRange,
} from "./filterMetrics";
import type { CandidateNote, DraftFilterRule } from "./types";

const note = (overrides: Partial<CandidateNote> = {}): CandidateNote => ({
  pitch: 64,
  start_us: 0,
  end_us: 500_000,
  velocity: 80,
  confidence: 0.7,
  track: 0,
  channel: 0,
  ...overrides,
});

const rule = (overrides: Partial<DraftFilterRule> = {}): DraftFilterRule => ({
  enabled: true,
  confidenceMin: "",
  confidenceMax: "",
  durationMin: "",
  durationMax: "",
  velocityMin: "",
  velocityMax: "",
  pitchMin: "",
  pitchMax: "",
  ...overrides,
});

describe("filter metrics", () => {
  it("clamps and quantizes values to the selected domain", () => {
    expect(quantizeMetric("pitch", 64.6)).toBe(65);
    expect(quantizeMetric("pitch", 140)).toBe(127);
    expect(quantizeMetric("velocity", -4)).toBe(1);
    expect(quantizeMetric("confidence", 0.734)).toBe(0.73);
  });

  it("creates a useful pitch range around the candidate distribution", () => {
    const range = suggestedMetricRange(
      [note({ pitch: 48 }), note({ pitch: 60 }), note({ pitch: 72 })],
      "pitch",
    );
    expect(range.lower).toBeLessThanOrEqual(48);
    expect(range.upper).toBeGreaterThanOrEqual(72);
  });

  it("creates a range around a dragged center", () => {
    const range = metricRangeAround([note({ pitch: 60 }), note({ pitch: 64 })], "pitch", 64);
    expect(range.lower).toBeLessThanOrEqual(64);
    expect(range.upper).toBeGreaterThanOrEqual(64);
  });

  it("parses only fields belonging to the requested metric", () => {
    expect(
      metricRuleRange(
        rule({ pitchMin: "40", pitchMax: "80", velocityMin: "20", velocityMax: "100" }),
        "pitch",
      ),
    ).toEqual({ lower: 40, upper: 80 });
    expect(metricRuleRange(rule(), "confidence")).toEqual({ lower: null, upper: null });
  });

  it("reports missing confidence values instead of inventing a value", () => {
    const definition = filterMetricDefinition("confidence");
    expect(definition.value(note({ confidence: null }))).toBeNull();
  });
});
