export type Operation = "transcribe" | "convert_midi" | "refilter" | "render_performance" | "edit_export";
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

export interface AnalysisRequest {
  input: string;
  output: string;
  audio_track: number | null;
  start_us: number | null;
  end_us: number | null;
  fft_size: number;
  hop_size: number;
  window: "hann" | "hamming" | "blackman";
  spectral: boolean;
  worker_path: string | null;
}

export interface AnalysisManifest {
  format_version: 1;
  cache_key: string;
  decode: {
    frames: number;
    duration_us: number;
    sample_rate: number;
    channels: number;
  };
  waveform: {
    base_bucket_count: number;
    levels: Array<{
      samples_per_bucket: number;
      bucket_count: number;
      offset_bytes: number;
      size_bytes: number;
    }>;
  };
  spectral?: {
    fft_size: number;
    hop_size: number;
    window: string;
    frames: number;
    bins: number;
  };
}

export interface AnalysisFinished {
  type: "result";
  directory: string;
  manifest_path: string;
  cache_key: string;
  cache_hit: boolean;
  duration_us: number;
  frames: number;
  sample_rate: number;
  channels: number;
}

export interface AnalysisProgress {
  type: "progress";
  stage: string;
  fraction: number | null;
}

export interface WaveformPayload {
  samples_per_bucket: number;
  bucket_count: number;
  data_base64: string;
}

export interface SpectrogramImage {
  width: number;
  height: number;
  data_base64: string;
}

export interface SpectrumFrame {
  frame: number;
  total_frames: number;
  hop_us: number;
  spectrum: number[];
  features: number[];
  columns: string[];
}

export interface PlaybackStatus {
  position_us: number;
  paused: boolean;
  available: boolean;
}

export interface SeparatorModelStatus {
  id: string;
  quality: string;
  logical_sha256: string;
  verified: boolean;
}

export interface SeparatorComponentStatus {
  installed: boolean;
  directory: string;
  component_id: string | null;
  component_version: string | null;
  models: SeparatorModelStatus[];
  error: string | null;
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

export type PitchBendClass = "stable" | "vibrato" | "slide" | "bend";

export interface PitchBendPoint {
  at_us: number;
  cents: number;
}

export interface PerformanceRevision {
  id: string;
  parent_id: string | null;
  source: "transcribe" | "convert_midi" | "refilter" | "render_performance" | "edit";
}

export interface PerformanceNote {
  id: string;
  start_us: number;
  end_us: number;
  key: string;
  pitch: number;
  velocity: number;
  confidence: number | null;
  source_stem: string;
  candidate_id: string | null;
  original_pitch: number;
  pitch_center: number;
  pitch_bend_class: PitchBendClass;
  pitch_bends: PitchBendPoint[];
}

export interface PerformanceDocument {
  format_version: 1;
  time_unit: "us";
  duration_us: number;
  revision: PerformanceRevision;
  source: {
    type: "audio" | "video" | "midi";
    offset_us: number;
  };
  mapping: {
    profile: string;
    transpose_semitones: number;
  };
  tempo_map: Array<{ at_us: number; bpm: number; source: string }>;
  beat_grid: Array<{
    at_us: number;
    beat_position: number;
    bpm: number;
    confidence: number | null;
  }>;
  notes: PerformanceNote[];
}

export interface CandidateNote {
  pitch: number;
  start_us: number;
  end_us: number;
  velocity: number;
  confidence: number | null;
  track: number;
  channel: number;
}

export interface StemArtifact {
  role: "vocals" | "drums" | "bass" | "other" | "instrumental";
  relative_path: string;
  sha256: string;
  size_bytes: number;
  sample_rate: 44100;
  channels: 2;
  duration_us: number;
}

export interface StemSetDocument {
  format_version: 1;
  source: { sha256: string };
  separation: {
    component_id: string;
    component_version: string;
    model_id: string;
    model_sha256: string;
    quality: "fast" | "balanced" | "high_quality";
  };
  sample_rate: 44100;
  channels: 2;
  stems: StemArtifact[];
  instrumental: StemArtifact;
}

export interface SeparationProgress {
  type: "progress";
  stage: string;
  fraction: number | null;
}

export interface SeparationFinished {
  type: "result";
  stem_set_path: string;
  elapsed_seconds: number;
}

export interface RoutingFinished {
  type: "result";
  routed_audio: string;
  routing_plan_path: string;
  mode: string;
  active_routes: number;
}
