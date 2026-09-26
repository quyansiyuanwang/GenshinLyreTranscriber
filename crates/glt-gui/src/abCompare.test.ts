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
