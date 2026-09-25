import { describe, expect, it } from "vitest";

import { formatRangeTime, normalizeRangeUs } from "./analysisSelection";

describe("analysis selection", () => {
  it("normalizes reversed and percent-based ranges", () => {
    expect(normalizeRangeUs(900, 100, 1000, 10)).toEqual({ startUs: 100, endUs: 900 });
  });

  it("clamps to the media duration", () => {
    expect(normalizeRangeUs(-50, 1200, 1000)).toEqual({ startUs: 0, endUs: 1000 });
  });

  it("keeps a minimum visible range at the tail", () => {
    expect(normalizeRangeUs(999, 999, 1000, 50)).toEqual({ startUs: 950, endUs: 1000 });
  });

  it("formats range labels", () => {
    expect(formatRangeTime(65_250_000)).toBe("01:05.25");
  });
});
