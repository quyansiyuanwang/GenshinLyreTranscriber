import { describe, expect, it } from "vitest";

import {
  DEFAULT_MAPPING_PROFILE,
  PORTABLE_CONFIG_VERSION,
  PortableConfigError,
  applyPortableConfig,
  createPortableConfig,
  defaultMappingKeys,
  parsePortableConfig,
  serializePortableConfig,
  shiftMappingKey,
  swapMappingKeys,
} from "./portableConfig";
import type { JobRequest } from "./types";

const request: JobRequest = {
  input: "input.flac",
  output: "output",
  operation: "transcribe",
  timing: "triplet",
  bpm: 132,
  transpose: 12,
  mapping_profile: "custom-c",
  mapping_keys: defaultMappingKeys().map((entry, index) => ({
    ...entry,
    pitch: entry.pitch + (index === 0 ? 0 : 0),
  })),
  audio_track: null,
  start_seconds: null,
  end_seconds: null,
  preview_wav: true,
  overwrite: false,
  cleaning_profile: "strict",
  min_confidence: 0.55,
  min_duration_ms: 120,
  retrigger_gap_ms: 25,
  arrangement: "balanced",
  onset_window_ms: 140,
  max_voices: 2,
  filter: null,
  filter_preset: null,
  worker_path: "worker.exe",
};

describe("portable config", () => {
  it("round-trips parameters and the 21-key mapping", () => {
    const config = createPortableConfig("我的琴", request);
    const parsed = parsePortableConfig(serializePortableConfig(config));
    expect(parsed.format_version).toBe(PORTABLE_CONFIG_VERSION);
    expect(parsed.mapping.profile).toBe("custom-c");
    expect(parsed.mapping.keys).toHaveLength(21);
    const applied = applyPortableConfig({ ...request, transpose: 0 }, parsed);
    expect(applied.transpose).toBe(12);
    expect(applied.mapping_keys).toEqual(parsed.mapping.keys);
  });

  it("uses the default mapping when the request has no custom layout", () => {
    const config = createPortableConfig("默认", { ...request, mapping_profile: null, mapping_keys: null });
    expect(config.mapping.profile).toBe(DEFAULT_MAPPING_PROFILE);
    expect(config.mapping.keys).toEqual(defaultMappingKeys());
  });

  it("rejects unknown versions and malformed mappings", () => {
    expect(() => parsePortableConfig('{"format_version":2}')).toThrow(PortableConfigError);
    const config = createPortableConfig("bad", request);
    config.mapping.keys[1] = { ...config.mapping.keys[0] };
    expect(() => parsePortableConfig(serializePortableConfig(config))).toThrow(
      "mapping keys must be unique",
    );
  });

  it("rejects out-of-range pitch values", () => {
    const config = createPortableConfig("bad", request);
    config.mapping.keys[0] = { key: "Z", pitch: 47 };
    expect(() => parsePortableConfig(serializePortableConfig(config))).toThrow(
      "outside C3-B5 naturals",
    );
  });

  it("swaps or moves pitches without duplicating a key", () => {
    const keys = defaultMappingKeys();
    const swapped = swapMappingKeys(keys, "Z", "X");
    expect(swapped[0].pitch).toBe(keys[1].pitch);
    expect(swapped[1].pitch).toBe(keys[0].pitch);
    const shifted = shiftMappingKey(keys, "Z", 12);
    expect(shifted[0].pitch).toBe(60);
    expect(shifted.find((entry) => entry.pitch === 60)?.key).toBe("Z");
  });
});
