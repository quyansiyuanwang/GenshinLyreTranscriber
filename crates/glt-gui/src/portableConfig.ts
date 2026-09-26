import {
  presetFromRequest,
  type DesktopPresetValues,
} from "./desktopPresets";
import type { JobRequest, MappingKey } from "./types";

export const PORTABLE_CONFIG_VERSION = 1;
export const DEFAULT_MAPPING_PROFILE = "lyre-21-default";
export const KEYBOARD_ORDER = "ZXCVBNMASDFGHJQWERTYU";
export const NATURAL_PITCHES = [
  48, 50, 52, 53, 55, 57, 59,
  60, 62, 64, 65, 67, 69, 71,
  72, 74, 76, 77, 79, 81, 83,
] as const;

export interface PortableConfigV1 {
  format_version: 1;
  name: string;
  parameters: DesktopPresetValues;
  mapping: {
    profile: string;
    keys: MappingKey[];
  };
}

export class PortableConfigError extends Error {}

export function defaultMappingKeys(): MappingKey[] {
  return [...KEYBOARD_ORDER].map((key, index) => ({ key, pitch: NATURAL_PITCHES[index] }));
}

export function pitchName(pitch: number): string {
  const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  return `${names[((pitch % 12) + 12) % 12]}${Math.floor(pitch / 12) - 1}`;
}

export function swapMappingKeys(
  keys: MappingKey[],
  firstKey: string,
  secondKey: string,
): MappingKey[] {
  const first = keys.find((entry) => entry.key === firstKey);
  const second = keys.find((entry) => entry.key === secondKey);
  if (!first || !second || firstKey === secondKey) return keys;
  return keys.map((entry) => {
    if (entry.key === firstKey) return { ...entry, pitch: second.pitch };
    if (entry.key === secondKey) return { ...entry, pitch: first.pitch };
    return entry;
  });
}

export function shiftMappingKey(
  keys: MappingKey[],
  key: string,
  semitones: number,
): MappingKey[] {
  const source = keys.find((entry) => entry.key === key);
  if (!source) return keys;
  const targetPitch = source.pitch + semitones;
  const target = keys.find((entry) => entry.pitch === targetPitch);
  if (target) return swapMappingKeys(keys, key, target.key);
  if (!NATURAL_PITCHES.includes(targetPitch as (typeof NATURAL_PITCHES)[number])) return keys;
  return keys.map((entry) => (entry.key === key ? { ...entry, pitch: targetPitch } : entry));
}

export function validateMappingKeys(value: unknown): MappingKey[] {
  if (!Array.isArray(value) || value.length !== 21) {
    throw new PortableConfigError("mapping.keys must contain exactly 21 entries");
  }
  const keys: MappingKey[] = value.map((entry) => {
    if (!entry || typeof entry !== "object") {
      throw new PortableConfigError("mapping key entry must be an object");
    }
    const candidate = entry as Partial<MappingKey>;
    if (typeof candidate.key !== "string" || !KEYBOARD_ORDER.includes(candidate.key)) {
      throw new PortableConfigError(`invalid lyre key: ${String(candidate.key)}`);
    }
    if (
      typeof candidate.pitch !== "number" ||
      !Number.isInteger(candidate.pitch) ||
      !NATURAL_PITCHES.includes(candidate.pitch as (typeof NATURAL_PITCHES)[number])
    ) {
      throw new PortableConfigError(
        `pitch ${String(candidate.pitch)} is outside C3-B5 naturals`,
      );
    }
    return { key: candidate.key, pitch: candidate.pitch };
  });
  if (new Set(keys.map((entry) => entry.key)).size !== 21) {
    throw new PortableConfigError("mapping keys must be unique");
  }
  if (new Set(keys.map((entry) => entry.pitch)).size !== 21) {
    throw new PortableConfigError("mapping pitches must be unique");
  }
  return keys;
}

function nullableNumber(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new PortableConfigError(`${label} must be null or ${minimum}..${maximum}`);
  }
  return value;
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[], label: string): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new PortableConfigError(`${label} is invalid`);
  }
  return value as T;
}

function validateParameters(value: unknown): DesktopPresetValues {
  if (!value || typeof value !== "object") {
    throw new PortableConfigError("parameters must be an object");
  }
  const parameters = value as Partial<DesktopPresetValues>;
  const transpose =
    parameters.transpose === "auto"
      ? "auto"
      : nullableNumber(parameters.transpose, "parameters.transpose", -48, 48);
  if (transpose === null) throw new PortableConfigError("parameters.transpose cannot be null");
  return {
    cleaning_profile: enumValue(
      parameters.cleaning_profile,
      ["auto", "solo", "mix", "strict"],
      "parameters.cleaning_profile",
    ),
    arrangement: enumValue(
      parameters.arrangement,
      ["balanced", "off"],
      "parameters.arrangement",
    ),
    min_confidence: nullableNumber(parameters.min_confidence, "parameters.min_confidence", 0, 1),
    min_duration_ms: nullableNumber(
      parameters.min_duration_ms,
      "parameters.min_duration_ms",
      0,
      3_600_000,
    ),
    retrigger_gap_ms: nullableNumber(
      parameters.retrigger_gap_ms,
      "parameters.retrigger_gap_ms",
      0,
      300_000,
    ),
    timing: enumValue(parameters.timing, ["auto", "preserve", "straight", "triplet"], "parameters.timing"),
    bpm: nullableNumber(parameters.bpm, "parameters.bpm", 1, 1000),
    transpose,
    onset_window_ms: nullableNumber(
      parameters.onset_window_ms,
      "parameters.onset_window_ms",
      1,
      10_000,
    ) ?? 150,
    max_voices:
      nullableNumber(parameters.max_voices, "parameters.max_voices", 1, 8) ?? 2,
    preview_wav: parameters.preview_wav === true,
  };
}

export function createPortableConfig(name: string, request: JobRequest): PortableConfigV1 {
  return {
    format_version: PORTABLE_CONFIG_VERSION,
    name: name.trim() || "未命名配置",
    parameters: presetFromRequest(request),
    mapping: {
      profile: request.mapping_profile || DEFAULT_MAPPING_PROFILE,
      keys: validateMappingKeys(request.mapping_keys ?? defaultMappingKeys()),
    },
  };
}

export function serializePortableConfig(config: PortableConfigV1): string {
  return `${JSON.stringify(config, null, 2)}\n`;
}

export function parsePortableConfig(raw: string): PortableConfigV1 {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new PortableConfigError("config is not valid JSON");
  }
  if (!value || typeof value !== "object") {
    throw new PortableConfigError("config root must be an object");
  }
  const candidate = value as Partial<PortableConfigV1>;
  if (candidate.format_version !== PORTABLE_CONFIG_VERSION) {
    throw new PortableConfigError(
      `unsupported config version: ${String(candidate.format_version)}`,
    );
  }
  if (typeof candidate.name !== "string" || !candidate.name.trim()) {
    throw new PortableConfigError("config name is required");
  }
  const mapping = candidate.mapping;
  if (!mapping || typeof mapping !== "object") {
    throw new PortableConfigError("mapping is required");
  }
  return {
    format_version: PORTABLE_CONFIG_VERSION,
    name: candidate.name.trim(),
    parameters: validateParameters(candidate.parameters),
    mapping: {
      profile:
        typeof mapping.profile === "string" && mapping.profile.trim()
          ? mapping.profile.trim()
          : DEFAULT_MAPPING_PROFILE,
      keys: validateMappingKeys(mapping.keys),
    },
  };
}

export function applyPortableConfig(
  request: JobRequest,
  config: PortableConfigV1,
): JobRequest {
  return {
    ...request,
    ...config.parameters,
    mapping_profile: config.mapping.profile,
    mapping_keys: config.mapping.keys,
  };
}
