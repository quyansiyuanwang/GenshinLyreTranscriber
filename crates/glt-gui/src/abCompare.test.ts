import { describe, expect, it } from "vitest";

import { abSwitchPosition, findAbSource, missingAbSourceMessage } from "./abCompare";
import type { PlaybackStatus } from "./types";

describe("A/B comparison", () => {
  it("keeps the live playback position when switching sources", () => {
    const status: PlaybackStatus = { position_us: 12_345_678, paused: false, available: true };
    expect(abSwitchPosition(status, 1_000)).toBe(12_345_678);
  });

  it("uses the UI fallback only when playback is unavailable", () => {
    const status: PlaybackStatus = { position_us: 0, paused: true, available: false };
    expect(abSwitchPosition(status, 2_500_000)).toBe(2_500_000);
  });

  it("does not silently substitute a missing source", () => {
    const option = { id: "original", label: "原音", path: null, primary: "original" as const };
    expect(findAbSource([option], "original")).toEqual(option);
    expect(missingAbSourceMessage(option)).toContain("原音");
  });
});
import { planAbSwitch, positionOnTimeline } from "./abCompare";
const hash = "a".repeat(64);
const original = { id: "a", label: "原音", path: "original.wav", primary: "original" as const, alignment: { sourceHash: hash, offsetUs: 0, durationUs: 120_000_000 } };
const excerpt = { id: "b", label: "片段", path: "preview.wav", primary: "mapped" as const, alignment: { sourceHash: hash, offsetUs: 30_000_000, durationUs: 20_000_000 } };
describe("A/B source-aware offsets", () => {
  it("maps original 35s to excerpt 5s and back", () => {
    expect(planAbSwitch(original, excerpt, 35_000_000, true)).toMatchObject({ positionUs: 5_000_000, aligned: true });
    expect(planAbSwitch(excerpt, original, 5_000_000, true)).toMatchObject({ positionUs: 35_000_000, aligned: true });
    expect(positionOnTimeline(35_000_000, original.alignment, hash, 30_000_000)).toBe(5_000_000);
  });
  it("clamps outside the common range with an explicit explanation", () => {
    expect(planAbSwitch(original, excerpt, 5_000_000, true)).toMatchObject({ positionUs: 0 });
    expect(planAbSwitch(original, excerpt, 55_000_000, true).message).toContain("有效边界");
    expect(planAbSwitch(original, excerpt, 55_000_000, true).positionUs).toBe(19_999_999);
  });
  it("never claims alignment for old data, different songs, or disjoint segments", () => {
    for (const target of [{ ...excerpt, alignment: undefined }, { ...excerpt, alignment: { ...excerpt.alignment, sourceHash: "b".repeat(64) } }, { ...excerpt, alignment: { ...excerpt.alignment, offsetUs: 140_000_000 } }]) {
      expect(planAbSwitch(original, target, 35_000_000, true)).toMatchObject({ positionUs: 0, aligned: false });
      expect(planAbSwitch(original, target, 35_000_000, true).message).toContain("独立试听");
    }
    expect(positionOnTimeline(5, original.alignment, "b".repeat(64), 0)).toBeNull();
  });
});

it("explicit replay restarts while A/B switching retains aligned time", () => {
  expect(planAbSwitch(original, excerpt, 35_000_000, true).positionUs).toBe(5_000_000);
  expect(planAbSwitch(excerpt, excerpt, 5_000_000, true, true).positionUs).toBe(0);
});
