import { describe, expect, it } from "vitest";

import { activePitchLines, liveFilterStats, matchesDraftRule, rulesToFilter } from "./filterPreview";
import type { CandidateNote, DraftFilterRule } from "./types";

function note(overrides: Partial<CandidateNote> = {}): CandidateNote {
  return {
    pitch: 64,
    start_us: 0,
    end_us: 200_000,
    velocity: 90,
    confidence: 0.8,
    track: 0,
    channel: 0,
    ...overrides,
  };
}

function rule(overrides: Partial<DraftFilterRule> = {}): DraftFilterRule {
  return {
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
  };
}

describe("live filter preview", () => {
  it("uses AND semantics inside a rule group", () => {
    const candidate = note();
    expect(matchesDraftRule(candidate, rule({ pitchMin: "60", pitchMax: "72", velocityMin: "80" }))).toBe(true);
    expect(matchesDraftRule(candidate, rule({ pitchMin: "60", pitchMax: "72", velocityMin: "100" }))).toBe(false);
  });

  it("rejects notes without confidence when confidence is constrained", () => {
    expect(matchesDraftRule(note({ confidence: null }), rule({ confidenceMin: "0.5" }))).toBe(false);
  });

  it("exposes pitch bounds for horizontal preview lines", () => {
    expect(activePitchLines([rule({ pitchMin: "48", pitchMax: "72" })])).toEqual([
      { ruleIndex: 0, min: 48, max: 72 },
    ]);
  });

  it("uses OR semantics across groups and reports removal", () => {
    const candidates = [note({ pitch: 60 }), note({ pitch: 70 }), note({ pitch: 80 })];
    const stats = liveFilterStats(candidates, [rule({ pitchMax: "64" }), rule({ pitchMin: "76" })]);
    expect(stats).toEqual({ total: 3, matched: 2, removed: 1, perRule: [1, 1], matches: [true, false, true] });
  });
});

it("exports no enabled rules as a reproducible unfiltered specification", () => {
  expect(rulesToFilter([])).toEqual({ format_version: 1, rules: [] });
  expect(rulesToFilter([rule({ enabled: false, pitchMin: "80", pitchMax: "40" })])).toEqual({ format_version: 1, rules: [] });
});
it("rejects invalid or reversed filter bounds instead of silently substituting defaults", () => {
  expect(() => rulesToFilter([rule({ confidenceMin: "bad" })])).toThrow();
  expect(() => rulesToFilter([rule({ pitchMin: "80", pitchMax: "40" })])).toThrow();
  expect(() => rulesToFilter([rule({ velocityMax: "128" })])).toThrow();
  expect(() => rulesToFilter([rule({ pitchMin: "60.5" })])).toThrow();
  expect(rulesToFilter([rule({ confidenceMin: "0", confidenceMax: "1" })]).rules[0].confidence).toEqual({ min: 0, max: 1 });
});
