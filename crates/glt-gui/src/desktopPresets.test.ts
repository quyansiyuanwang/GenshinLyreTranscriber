import { describe, expect, it } from "vitest";

import { BUILTIN_PRESETS, loadCustomPresets, nextCustomPresetName } from "./desktopPresets";

describe("desktop presets", () => {
  it("ignores malformed persisted entries", () => {
    expect(loadCustomPresets("bad-json")).toEqual([]);
    expect(loadCustomPresets('[{"id":"x","name":"x","builtin":true,"values":{}}]')).toEqual([]);
  });

  it("chooses the next unused custom name", () => {
    expect(nextCustomPresetName([])).toBe("自定义 1");
    expect(
      nextCustomPresetName([
        { id: "a", name: "自定义 1", builtin: false, values: {} as never },
        { id: "b", name: "自定义 3", builtin: false, values: {} as never },
      ]),
    ).toBe("自定义 2");
  });

  it("offers a melody-recall preset with more voices and lower thresholds", () => {
    const preset = BUILTIN_PRESETS.find((item) => item.id === "builtin-melody-recall");
    expect(preset).toBeDefined();
    expect(preset?.values.min_confidence).toBe(0.15);
    expect(preset?.values.min_duration_ms).toBe(40);
    expect(preset?.values.max_voices).toBe(3);
  });
});
