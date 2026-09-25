import type { CandidateNote, DraftFilterRule, FilterRule, FilterSpec } from "./types";

export interface LiveFilterStats {
  total: number;
  matched: number;
  removed: number;
  perRule: number[];
  matches: boolean[];
}

export function parseOptionalNumber(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

export function hasRuleValue(rule: DraftFilterRule): boolean {
  return Object.entries(rule).some(([key, value]) => key !== "enabled" && value !== "");
}

export function range(
  minimum: string,
  maximum: string,
  fallbackMin: number,
  fallbackMax: number,
): { min: number; max: number } | undefined {
  if (!minimum.trim() && !maximum.trim()) return undefined;
  return {
    min: parseOptionalNumber(minimum) ?? fallbackMin,
    max: parseOptionalNumber(maximum) ?? fallbackMax,
  };
}

export function rulesToFilter(rules: DraftFilterRule[]): FilterSpec | null {
  const converted: FilterRule[] = [];
  for (const rule of rules) {
    if (!rule.enabled || !hasRuleValue(rule)) continue;
    converted.push({
      enabled: true,
      confidence: range(rule.confidenceMin, rule.confidenceMax, 0, 1),
      duration_ms: range(rule.durationMin, rule.durationMax, 0, 3_600_000),
      velocity: range(rule.velocityMin, rule.velocityMax, 1, 127),
      pitch: range(rule.pitchMin, rule.pitchMax, 0, 127),
    });
  }
  return converted.length ? { format_version: 1, rules: converted } : null;
}

function within(value: number, limits: { min: number; max: number } | undefined): boolean {
  return limits === undefined || (value >= limits.min && value <= limits.max);
}

export function matchesDraftRule(note: CandidateNote, rule: DraftFilterRule): boolean {
  if (!rule.enabled || !hasRuleValue(rule)) return false;
  const durationMs = (note.end_us - note.start_us) / 1000;
  const confidence =
    rule.confidenceMin || rule.confidenceMax
      ? range(rule.confidenceMin, rule.confidenceMax, 0, 1)
      : undefined;
  if (confidence && note.confidence === null) return false;
  return (
    within(note.confidence ?? -1, confidence) &&
    within(durationMs, range(rule.durationMin, rule.durationMax, 0, 3_600_000)) &&
    within(note.velocity, range(rule.velocityMin, rule.velocityMax, 1, 127)) &&
    within(note.pitch, range(rule.pitchMin, rule.pitchMax, 0, 127))
  );
}

export function liveFilterStats(
  notes: CandidateNote[],
  rules: DraftFilterRule[],
): LiveFilterStats {
  const activeRules = rules.filter((rule) => rule.enabled && hasRuleValue(rule));
  const perRule = rules.map((rule) =>
    rule.enabled && hasRuleValue(rule)
      ? notes.filter((note) => matchesDraftRule(note, rule)).length
      : 0,
  );
  const matches = notes.map(
    (note) => activeRules.length === 0 || activeRules.some((rule) => matchesDraftRule(note, rule)),
  );
  const matched = matches.filter(Boolean).length;
  return {
    total: notes.length,
    matched,
    removed: notes.length - matched,
    perRule,
    matches,
  };
}

export interface ActivePitchLine {
  ruleIndex: number;
  min: number | null;
  max: number | null;
}

export function activePitchLines(rules: DraftFilterRule[]): ActivePitchLine[] {
  return rules
    .map((rule, ruleIndex) => ({ rule, ruleIndex }))
    .filter(({ rule }) => rule.enabled && hasRuleValue(rule))
    .map(({ rule, ruleIndex }) => ({
      ruleIndex,
      min: parseOptionalNumber(rule.pitchMin),
      max: parseOptionalNumber(rule.pitchMax),
    }))
    .filter((line) => line.min !== null || line.max !== null);
}
