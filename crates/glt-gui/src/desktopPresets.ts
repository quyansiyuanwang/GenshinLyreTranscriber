import type {
  ArrangementProfile,
  CleaningProfile,
  JobRequest,
  Timing,
  Transpose,
} from "./types";

export interface DesktopPresetValues {
  cleaning_profile: CleaningProfile;
  arrangement: ArrangementProfile;
  min_confidence: number | null;
  min_duration_ms: number | null;
  retrigger_gap_ms: number | null;
  timing: Timing;
  bpm: number | null;
  transpose: Transpose;
  onset_window_ms: number;
  max_voices: number;
  preview_wav: boolean;
}

export interface DesktopPreset {
  id: string;
  name: string;
  builtin: boolean;
  values: DesktopPresetValues;
}

const base: DesktopPresetValues = {
  cleaning_profile: "auto",
  arrangement: "balanced",
  min_confidence: null,
  min_duration_ms: null,
  retrigger_gap_ms: null,
  timing: "auto",
  bpm: null,
  transpose: "auto",
  onset_window_ms: 150,
  max_voices: 2,
  preview_wav: true,
};

export const BUILTIN_PRESETS: DesktopPreset[] = [
  { id: "builtin-auto", name: "自动平衡", builtin: true, values: { ...base } },
  {
    id: "builtin-solo",
    name: "独奏优先",
    builtin: true,
    values: { ...base, cleaning_profile: "solo", min_confidence: 0.2, min_duration_ms: 50 },
  },
  {
    id: "builtin-mix",
    name: "混音降噪",
    builtin: true,
    values: { ...base, cleaning_profile: "mix", min_confidence: 0.4, min_duration_ms: 100 },
  },
  {
    id: "builtin-strict",
    name: "严格清理",
    builtin: true,
    values: { ...base, cleaning_profile: "strict", min_confidence: 0.5, min_duration_ms: 150 },
  },
  {
    id: "builtin-melody-recall",
    name: "主旋律增强",
    builtin: true,
    values: {
      ...base,
      cleaning_profile: "solo",
      min_confidence: 0.15,
      min_duration_ms: 40,
      max_voices: 3,
    },
  },
];

export function presetFromRequest(request: JobRequest): DesktopPresetValues {
  return {
    cleaning_profile: request.cleaning_profile,
    arrangement: request.arrangement,
    min_confidence: request.min_confidence,
    min_duration_ms: request.min_duration_ms,
    retrigger_gap_ms: request.retrigger_gap_ms,
    timing: request.timing,
    bpm: request.bpm,
    transpose: request.transpose,
    onset_window_ms: request.onset_window_ms,
    max_voices: request.max_voices,
    preview_wav: request.preview_wav,
  };
}

export function loadCustomPresets(value: string | null): DesktopPreset[] {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is DesktopPreset =>
        typeof item === "object" &&
        item !== null &&
        typeof item.id === "string" &&
        typeof item.name === "string" &&
        item.builtin === false &&
        typeof item.values === "object" &&
        item.values !== null,
    );
  } catch {
    return [];
  }
}

export function nextCustomPresetName(presets: DesktopPreset[]): string {
  const used = new Set(presets.map((preset) => preset.name));
  let index = 1;
  while (used.has(`自定义 ${index}`)) index += 1;
  return `自定义 ${index}`;
}
