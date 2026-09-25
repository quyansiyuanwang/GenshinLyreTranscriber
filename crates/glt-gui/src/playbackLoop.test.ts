import { describe, expect, it } from "vitest";

import { shouldLoopSeek } from "./playbackLoop";

describe("selection playback loop", () => {
  it("seeks only when playing past the selected end", () => {
    const range = { startUs: 1_000_000, endUs: 3_000_000 };
    expect(shouldLoopSeek(3_000_000, range, true, false, true)).toBe(true);
    expect(shouldLoopSeek(2_900_000, range, true, false, true)).toBe(false);
  });

  it("stays disabled for paused, unavailable or empty ranges", () => {
    const range = { startUs: 1_000_000, endUs: 1_000_000 };
    expect(shouldLoopSeek(3_000_000, range, true, false, true)).toBe(false);
    expect(shouldLoopSeek(3_000_000, { startUs: 1, endUs: 2 }, true, true, true)).toBe(false);
    expect(shouldLoopSeek(3_000_000, { startUs: 1, endUs: 2 }, true, false, false)).toBe(false);
  });
});
