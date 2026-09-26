import { describe, expect, it } from "vitest";

import {
  BUILTIN_PRESETS,
  loadCustomPresets,
  nextCustomPresetName,
  presetMatchesRequest,
} from "./desktopPresets";
import type { JobRequest } from "./types";

const REQUEST: JobRequest = {
  input: "song.flac",
  output: "song-output",
  operation: "transcribe",
  timing: "auto",
  bpm: null,
  transpose: "auto",
  mapping_profile: null,
  mapping_keys: null,
  audio_track: null,
  start_seconds: null,
  end_seconds: null,
  preview_wav: true,
  overwrite: false,
  cleaning_profile: "auto",
  min_confidence: null,
  min_duration_ms: null,
  retrigger_gap_ms: null,
  arrangement: "balanced",
  onset_window_ms: 150,
  max_voices: 2,
  filter: null,
  filter_preset: null,
  worker_path: null,
};

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

  it("marks the current parameter combination as the active preset", () => {
    expect(presetMatchesRequest(BUILTIN_PRESETS[0], REQUEST)).toBe(true);
    expect(
      presetMatchesRequest(BUILTIN_PRESETS[1], {
        ...REQUEST,
        min_confidence: 0.2,
        min_duration_ms: 50,
        cleaning_profile: "solo",
      }),
    ).toBe(true);
    expect(
      presetMatchesRequest(BUILTIN_PRESETS[1], {
        ...REQUEST,
        min_confidence: 0.21,
        min_duration_ms: 50,
        cleaning_profile: "solo",
      }),
    ).toBe(false);
  });
});
