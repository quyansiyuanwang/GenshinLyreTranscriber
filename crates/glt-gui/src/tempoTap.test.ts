import { describe, expect, it } from "vitest";

import { estimateBpm, recentTapTimes } from "./tempoTap";

describe("tap tempo", () => {
  it("keeps only recent taps", () => {
    expect(recentTapTimes([0, 500, 1000, 5000], 5600)).toEqual([5000, 5600]);
  });

  it("estimates BPM from stable taps", () => {
    expect(estimateBpm([0, 500, 1000, 1500, 2000])).toBe(120);
  });

  it("rejects a single or unstable tap sequence", () => {
    expect(estimateBpm([0])).toBeNull();
    expect(estimateBpm([0, 1])).toBeNull();
  });
});
