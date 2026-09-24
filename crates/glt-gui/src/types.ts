export type Operation = "transcribe" | "convert_midi" | "refilter";
export type Timing = "auto" | "preserve" | "straight" | "triplet";
export type Transpose = "auto" | number;
export type CleaningProfile = "auto" | "solo" | "mix" | "strict";
export type ArrangementProfile = "balanced" | "off";
export type FilterPreset = "off" | "auto" | "balanced" | "melody";

export interface FilterRange {
  min: number;
  max: number;
}

export interface IntegerFilterRange {
  min: number;
  max: number;
}

export interface FilterRule {
  enabled: boolean;
  confidence?: FilterRange;
  duration_ms?: FilterRange;
  velocity?: IntegerFilterRange;
  pitch?: IntegerFilterRange;
}

export interface FilterSpec {
  format_version: 1;
  rules: FilterRule[];
}

export interface JobRequest {
  input: string;
  output: string;
  operation: Operation;
  timing: Timing;
  bpm: number | null;
  transpose: Transpose;
  audio_track: number | null;
  start_seconds: number | null;
  end_seconds: number | null;
  preview_wav: boolean;
  overwrite: boolean;
  cleaning_profile: CleaningProfile;
  min_confidence: number | null;
  min_duration_ms: number | null;
  retrigger_gap_ms: number | null;
  arrangement: ArrangementProfile;
  onset_window_ms: number;
  max_voices: number;
  filter: FilterSpec | null;
  filter_preset: FilterPreset | null;
  worker_path: string | null;
}

export interface Artifact {
  kind: string;
  relative_path: string;
  sha256: string;
  size_bytes: number;
}

export interface JobResult {
  job_id: string;
  result: {
    output_dir: string;
    report_path: string;
    artifacts: Artifact[];
  };
}

export type JobEvent =
  | { type: "progress"; stage: string; fraction: number | null }
  | { type: "warning"; code: string; message: string };

export interface DoctorInfo {
  worker_version: string;
  application_version: string;
  model_version: string;
}

export interface ProjectRevision {
  id: string;
  kind: string;
  relative_path: string;
  parent_id: string | null;
  created_at: string;
}

export interface ProjectDocument {
  format_version: 1;
  project_id: string;
  name: string;
  created_at: string;
  updated_at: string;
  source: {
    path: string;
    sha256: string | null;
    size_bytes: number | null;
    modified_unix_ms: number | null;
  } | null;
  revisions: ProjectRevision[];
}

export interface ReportDocument {
  schema_version?: number;
  parameters?: Record<string, unknown>;
  counts?: Record<string, number>;
  warnings?: Array<{ code?: string; message?: string }>;
  artifacts?: Artifact[];
  [key: string]: unknown;
}

export interface DraftFilterRule {
  enabled: boolean;
  confidenceMin: string;
  confidenceMax: string;
  durationMin: string;
  durationMax: string;
  velocityMin: string;
  velocityMax: string;
  pitchMin: string;
  pitchMax: string;
}
