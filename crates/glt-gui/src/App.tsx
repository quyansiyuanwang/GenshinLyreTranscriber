import { useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { appDataDir, join } from "@tauri-apps/api/path";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import { openPath } from "@tauri-apps/plugin-opener";

import type {
  AnalysisFinished,
  AnalysisManifest,
  AnalysisProgress,
  DoctorInfo,
  DraftFilterRule,
  FilterPreset,
  JobEvent,
  JobRequest,
  JobResult,
  MediaProbeDocument,
  Operation,
  CandidateNote,
  PerformanceDocument,
  ProjectDocument,
  PlaybackStatus,
  ReportDocument,
  RoutingFinished,
  SeparationFinished,
  SeparationProgress,
  SeparatorComponentStatus,
  StemSetDocument,
  SpectrogramImage,
  SpectrumFrame,
  WaveformPayload,
} from "./types";
import AnalysisView from "./AnalysisView";
import FilterPreviewCanvas from "./FilterPreviewCanvas";
import FilterRangeSlider from "./FilterRangeSlider";
import { liveFilterStats, parseOptionalNumber, rulesToFilter } from "./filterPreview";
import ParameterSlider from "./ParameterSlider";
import { addRecentPath, parseRecentPaths } from "./recentPaths";
import { shouldLoopSeek } from "./playbackLoop";
import PianoRollEditor from "./PianoRollEditor";
import SegmentedControl from "./SegmentedControl";
import { buildCustomRoutingPlan, defaultStemRoute, type StemRouteControl, type StemTarget } from "./routingPlan";
import { BUILTIN_PRESETS, loadCustomPresets, nextCustomPresetName, presetFromRequest } from "./desktopPresets";
import { estimateBpm, recentTapTimes } from "./tempoTap";

type NumericDraftFilterKey = Exclude<keyof DraftFilterRule, "enabled">;

const MEDIA_FILTERS = [
  {
    name: "音频与视频",
    extensions: ["flac", "wav", "mp3", "m4a", "aac", "ogg", "opus", "mp4", "mkv", "mov", "webm"],
  },
  { name: "MIDI", extensions: ["mid", "midi"] },
  { name: "全部文件", extensions: ["*"] },
];

const EMPTY_RULE: DraftFilterRule = {
  enabled: true,
  confidenceMin: "",
  confidenceMax: "",
  durationMin: "",
  durationMax: "",
  velocityMin: "",
  velocityMax: "",
  pitchMin: "",
  pitchMax: "",
};

const FILTER_FIELDS = [
  {
    minimumKey: "confidenceMin",
    maximumKey: "confidenceMax",
    label: "confidence",
    minimum: 0,
    maximum: 1,
    step: 0.01,
    lowerFallback: 0,
    upperFallback: 1,
    unit: "",
  },
  {
    minimumKey: "durationMin",
    maximumKey: "durationMax",
    label: "duration",
    minimum: 0,
    maximum: 10000,
    step: 50,
    lowerFallback: 0,
    upperFallback: 10000,
    unit: "ms",
  },
  {
    minimumKey: "velocityMin",
    maximumKey: "velocityMax",
    label: "velocity",
    minimum: 1,
    maximum: 127,
    step: 1,
    lowerFallback: 1,
    upperFallback: 127,
    unit: "",
  },
  {
    minimumKey: "pitchMin",
    maximumKey: "pitchMax",
    label: "MIDI pitch",
    minimum: 0,
    maximum: 127,
    step: 1,
    lowerFallback: 0,
    upperFallback: 127,
    unit: "",
  },
] as const;

const STEM_TARGETS: Array<{ value: StemTarget; label: string; title: string }> = [
  { value: "melody", label: "旋", title: "旋律 melody" },
  { value: "harmony", label: "和", title: "和声 harmony" },
  { value: "bass", label: "低", title: "低音 bass" },
  { value: "percussion", label: "打", title: "打击乐 percussion" },
  { value: "ignore", label: "×", title: "忽略 ignore" },
];

const DEFAULT_REQUEST: JobRequest = {
  input: "",
  output: "",
  operation: "transcribe",
  timing: "auto",
  bpm: null,
  transpose: "auto",
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

function isMidi(path: string): boolean {
  return /\.(mid|midi)$/i.test(path);
}

function operationForPath(path: string): Operation {
  return isMidi(path) ? "convert_midi" : "transcribe";
}

async function defaultOutputFor(input: string): Promise<string> {
  const separator = Math.max(input.lastIndexOf("/"), input.lastIndexOf("\\"));
  const parent = separator >= 0 ? input.slice(0, separator) : ".";
  const filename = separator >= 0 ? input.slice(separator + 1) : input;
  const stem = filename.replace(/\.[^.]+$/, "") || filename;
  return join(parent, `${stem}-output`);
}

function parentPath(path: string): string {
  const separator = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return separator >= 0 ? path.slice(0, separator) : ".";
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function countLabel(key: string): string {
  const labels: Record<string, string> = {
    input_notes: "输入音符",
    output_notes: "输出音符",
    dropped_notes: "删除音符",
    mapped_keys: "映射按键",
    replaced_semitones: "半音替换",
    octave_folds: "八度折返",
    duplicate_keys: "同刻去重",
    compatibility_collisions: "兼容谱碰撞",
  };
  return labels[key] ?? key;
}

function formatClock(valueUs: number): string {
  const totalSeconds = Math.max(0, valueUs) / 1_000_000;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.floor(totalSeconds % 60);
  const milliseconds = Math.floor((totalSeconds % 1) * 1000);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
}

function stemLabel(role: string): string {
  const labels: Record<string, string> = {
    vocals: "人声",
    drums: "鼓组",
    bass: "低音",
    other: "其他",
    instrumental: "伴奏",
  };
  return labels[role] ?? role;
}

function loadRecentPaths(key: string): string[] {
  if (typeof window === "undefined") return [];
  return parseRecentPaths(window.localStorage.getItem(key));
}

function App() {
  const [request, setRequest] = useState<JobRequest>(DEFAULT_REQUEST);
  const [customPresets, setCustomPresets] = useState(() =>
    loadCustomPresets(
      typeof window === "undefined" ? null : window.localStorage.getItem("glt.customPresets"),
    ),
  );
  const [recentInputs, setRecentInputs] = useState<string[]>(() =>
    loadRecentPaths("glt.recentInputs"),
  );
  const [recentOutputs, setRecentOutputs] = useState<string[]>(() =>
    loadRecentPaths("glt.recentOutputs"),
  );
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterRules, setFilterRules] = useState<DraftFilterRule[]>([{ ...EMPTY_RULE }]);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState("待机");
  const [fraction, setFraction] = useState<number | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("准备就绪");
  const [toast, setToast] = useState<string | null>(null);
  const [result, setResult] = useState<JobResult | null>(null);
  const [report, setReport] = useState<ReportDocument | null>(null);
  const [performance, setPerformance] = useState<PerformanceDocument | null>(null);
  const [candidateNotes, setCandidateNotes] = useState<CandidateNote[]>([]);
  const [editApplying, setEditApplying] = useState(false);
  const [abSource, setAbSource] = useState("preview");
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [separationRunning, setSeparationRunning] = useState(false);
  const [separationStage, setSeparationStage] = useState("idle");
  const [separationFraction, setSeparationFraction] = useState<number | null>(null);
  const [stemSetPath, setStemSetPath] = useState<string | null>(null);
  const [stemSet, setStemSet] = useState<StemSetDocument | null>(null);
  const [routingRunning, setRoutingRunning] = useState(false);
  const [routingMode, setRoutingMode] = useState("solo");
  const [stemRoutes, setStemRoutes] = useState<Record<string, StemRouteControl>>({});
  const tapTimesRef = useRef<number[]>([]);
  const [tapCount, setTapCount] = useState(0);
  const [routedAudioPath, setRoutedAudioPath] = useState<string | null>(null);
  const [doctor, setDoctor] = useState<DoctorInfo | null>(null);
  const [mediaProbe, setMediaProbe] = useState<MediaProbeDocument | null>(null);
  const [mediaProbeError, setMediaProbeError] = useState<string | null>(null);
  const [mediaProbeRunning, setMediaProbeRunning] = useState(false);
  const [project, setProject] = useState<ProjectDocument | null>(null);
  const [separatorStatus, setSeparatorStatus] = useState<SeparatorComponentStatus | null>(null);
  const [separatorDirectory, setSeparatorDirectory] = useState<string | null>(null);
  const [analysisManifest, setAnalysisManifest] = useState<AnalysisManifest | null>(null);
  const [analysisDirectory, setAnalysisDirectory] = useState<string | null>(null);
  const [analysisWaveform, setAnalysisWaveform] = useState<WaveformPayload | null>(null);
  const [analysisSpectrogram, setAnalysisSpectrogram] = useState<SpectrogramImage | null>(null);
  const [analysisSpectrum, setAnalysisSpectrum] = useState<SpectrumFrame | null>(null);
  const [analysisRunning, setAnalysisRunning] = useState(false);
  const [analysisStage, setAnalysisStage] = useState("idle");
  const [analysisFraction, setAnalysisFraction] = useState<number | null>(null);
  const [positionUs, setPositionUs] = useState(0);
  const [playback, setPlayback] = useState("idle");
  const [loopEnabled, setLoopEnabled] = useState(false);
  const loopSeekingRef = useRef(false);
  const [volume, setVolume] = useState(0.8);
  const jobRequestRef = useRef(request);
  jobRequestRef.current = request;
  const pendingRevisionRef = useRef<{
    kind: string;
    path: string;
    parentId: string | null;
  } | null>(null);

  const previewArtifact = result?.result.artifacts.find(
    (artifact) => artifact.kind === "preview_wav",
  );

  const counts = useMemo(() => Object.entries(report?.counts ?? {}), [report]);
  const filterPreview = useMemo(
    () => liveFilterStats(candidateNotes, filterRules),
    [candidateNotes, filterRules],
  );
  const abOptions = useMemo(() => {
    const options = [
      {
        id: "preview",
        label: "合成",
        path: previewArtifact && result
          ? `${result.result.output_dir}\\${previewArtifact.relative_path}`
          : null,
      },
      { id: "original", label: "原音", path: request.input || null },
    ];
    for (const artifact of result?.result.artifacts ?? []) {
      if (artifact.kind === "instrumental_wav" || artifact.kind === "routed_audio") {
        options.push({
          id: artifact.kind,
          label: artifact.kind === "instrumental_wav" ? "Instrumental" : "路由",
          path: `${result!.result.output_dir}\\${artifact.relative_path}`,
        });
      }
    }
    if (stemSet && stemSetPath) {
      for (const stem of stemSet.stems) {
        options.push({
          id: `stem-${stem.role}`,
          label: stemLabel(stem.role),
          path: `${parentPath(stemSetPath)}\\${stem.relative_path}`,
        });
      }
      options.push({
        id: "instrumental",
        label: "Instrumental",
        path: `${parentPath(stemSetPath)}\\${stemSet.instrumental.relative_path}`,
      });
    }
    if (routedAudioPath) {
      options.push({ id: "routed_audio", label: "路由", path: routedAudioPath });
    }
    return options;
  }, [previewArtifact, request.input, result, routedAudioPath, stemSet, stemSetPath]);
  const hasActiveAbSource = abOptions.some((option) => option.id === abSource && option.path);

  useEffect(() => {
    try {
      window.localStorage.setItem("glt.recentInputs", JSON.stringify(recentInputs));
      window.localStorage.setItem("glt.recentOutputs", JSON.stringify(recentOutputs));
      window.localStorage.setItem("glt.customPresets", JSON.stringify(customPresets));
    } catch {
      // Recent paths are a convenience; storage failure must not block work.
    }
  }, [customPresets, recentInputs, recentOutputs]);

  useEffect(() => {
    invoke<ProjectDocument | null>("project_current")
      .then((document) => {
        setProject(document);
        if (document?.source?.path) {
          setRequest((current) => ({ ...current, input: document.source!.path }));
        }
      })
      .catch((reason) => setNotice(`project: ${String(reason)}`));
    invoke<DoctorInfo>("doctor")
      .then(setDoctor)
      .catch((reason) => setNotice(`worker: ${String(reason)}`));
    void appDataDir()
      .then((root) => join(root, "separator"))
      .then(async (directory) => {
        setSeparatorDirectory(directory);
        setSeparatorStatus(
          await invoke<SeparatorComponentStatus>("separator_component_status", { directory }),
        );
      })
      .catch((reason) => setNotice(`separator: ${String(reason)}`));
  }, []);

  useEffect(() => {
    setToast(notice);
    if (!notice) return;
    const timer = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!request.input || isMidi(request.input)) {
      setMediaProbe(null);
      setMediaProbeError(null);
      setMediaProbeRunning(false);
      return;
    }
    let disposed = false;
    setMediaProbeRunning(true);
    setMediaProbeError(null);
    const timer = window.setTimeout(() => {
      void invoke<MediaProbeDocument>("probe_media", {
        input: request.input,
        workerPath: request.worker_path,
      })
        .then((document) => {
          if (!disposed) setMediaProbe(document);
        })
        .catch((reason) => {
          if (!disposed) {
            setMediaProbe(null);
            setMediaProbeError(String(reason));
          }
        })
        .finally(() => {
          if (!disposed) setMediaProbeRunning(false);
        });
    }, 250);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [request.input, request.worker_path]);

  useEffect(() => {
    const directory = result?.result.output_dir;
    if (!directory) {
      setPerformance(null);
      setCandidateNotes([]);
      return;
    }
    void invoke<PerformanceDocument>("read_performance", { resultDir: directory })
      .then(setPerformance)
      .catch((reason) => {
        setPerformance(null);
        setNotice(`performance: ${String(reason)}`);
      });
    void invoke<CandidateNote[]>("read_candidate_overlay", { resultDir: directory })
      .then(setCandidateNotes)
      .catch(() => setCandidateNotes([]));
  }, [result?.result.output_dir]);

  useEffect(() => {
    const unlisteners: Array<() => void> = [];
    let disposed = false;

    void listen<JobEvent>("job-event", ({ payload }) => {
      if (payload.type === "progress") {
        setStage(payload.stage);
        setFraction(payload.fraction);
      } else {
        setWarnings((current) => [...current, `[${payload.code}] ${payload.message}`]);
      }
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<JobResult>("job-finished", ({ payload }) => {
      setRunning(false);
      setEditApplying(false);
      setFraction(1);
      setStage("completed");
      setResult(payload);
      setRecentOutputs((current) => addRecentPath(current, payload.result.output_dir));
      setPlayback("idle");
      setNotice("转换完成");
      void invoke<ReportDocument>("read_report", { resultDir: payload.result.output_dir })
        .then(setReport)
        .catch((reason) => setError(String(reason)));
      const pending = pendingRevisionRef.current;
      if (pending && pending.path === payload.result.output_dir) {
        pendingRevisionRef.current = null;
        void recordRevision(pending.kind, pending.path, pending.parentId);
      }
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<string>("job-failed", ({ payload }) => {
      setRunning(false);
      setEditApplying(false);
      setStage("failed");
      setNotice("任务失败");
      setError(payload);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen("job-cancelled", () => {
      setRunning(false);
      setEditApplying(false);
      setStage("cancelled");
      setNotice("任务已取消");
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<SeparationProgress>("separation-progress", ({ payload }) => {
      setSeparationRunning(payload.stage !== "completed");
      setSeparationStage(payload.stage);
      setSeparationFraction(payload.fraction);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<SeparationFinished>("separation-finished", ({ payload }) => {
      setSeparationRunning(false);
      setSeparationStage("completed");
      setSeparationFraction(1);
      setStemSetPath(payload.stem_set_path);
      setNotice("Stem 分离完成");
      void invoke<StemSetDocument>("read_stem_set", { path: payload.stem_set_path })
        .then((document) => {
          setStemSet(document);
          setStemRoutes(
            Object.fromEntries(
              document.stems.map((stem) => [stem.role, defaultStemRoute(stem.role)]),
            ),
          );
          void recordRevision("stems", parentPath(payload.stem_set_path), null);
        })
        .catch((reason) => setError(String(reason)));
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<string>("separation-failed", ({ payload }) => {
      setSeparationRunning(false);
      setSeparationStage("failed");
      setError(`分离失败：${payload}`);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<SeparationProgress>("routing-progress", ({ payload }) => {
      setRoutingRunning(payload.stage !== "completed");
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<RoutingFinished>("routing-finished", ({ payload }) => {
      setRoutingRunning(false);
      setRoutedAudioPath(payload.routed_audio);
      setNotice(`${payload.mode} 路由试听已生成`);
      void recordRevision("routing-preview", parentPath(payload.routing_plan_path), null);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<string>("routing-failed", ({ payload }) => {
      setRoutingRunning(false);
      setError(`路由失败：${payload}`);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<AnalysisProgress>("analysis-progress", ({ payload }) => {
      setAnalysisRunning(payload.stage !== "completed");
      setAnalysisStage(payload.stage);
      setAnalysisFraction(payload.fraction);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<AnalysisFinished>("analysis-finished", ({ payload }) => {
      setAnalysisRunning(false);
      setAnalysisDirectory(payload.directory);
      setNotice(payload.cache_hit ? "分析缓存已加载" : "音频分析完成");
      void loadAnalysisViews(payload.directory);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    void listen<string>("analysis-failed", ({ payload }) => {
      setAnalysisRunning(false);
      setAnalysisStage("failed");
      setError(`分析失败：${payload}`);
    }).then((unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)));

    return () => {
      disposed = true;
      for (const unlisten of unlisteners) unlisten();
    };
  }, []);

  useEffect(() => {
    if (!analysisManifest?.spectral || !analysisDirectory) return;
    const timer = window.setInterval(() => {
      void invoke<PlaybackStatus>("playback_status")
        .then(async (status) => {
          setPositionUs(status.position_us);
          const range =
            request.start_seconds !== null && request.end_seconds !== null
              ? {
                  startUs: Math.round(request.start_seconds * 1_000_000),
                  endUs: Math.round(request.end_seconds * 1_000_000),
                }
              : null;
          if (
            !loopSeekingRef.current &&
            shouldLoopSeek(
              status.position_us,
              range,
              loopEnabled,
              status.paused,
              status.available,
            )
          ) {
            loopSeekingRef.current = true;
            try {
              await invoke("seek_playback", { positionUs: range!.startUs });
              setPositionUs(range!.startUs);
              setNotice("循环选区：已回到起点");
            } finally {
              window.setTimeout(() => {
                loopSeekingRef.current = false;
              }, 120);
            }
          }
          if (status.paused || !status.available) return;
          const frame = Math.min(
            analysisManifest.spectral!.frames - 1,
            Math.max(
              0,
              Math.round(
                status.position_us /
                  ((analysisManifest.spectral!.hop_size * 1_000_000) /
                    analysisManifest.decode.sample_rate),
              ),
            ),
          );
          const spectrum = await invoke<SpectrumFrame>("analysis_spectrum", {
            directory: analysisDirectory,
            frame,
          });
          setAnalysisSpectrum(spectrum);
        })
        .catch(() => undefined);
    }, 50);
    return () => window.clearInterval(timer);
  }, [
    analysisDirectory,
    analysisManifest,
    loopEnabled,
    request.end_seconds,
    request.start_seconds,
  ]);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void getCurrentWebview()
      .onDragDropEvent((event) => {
        if (event.payload.type !== "drop" || event.payload.paths.length === 0) return;
        const path = event.payload.paths[0];
        void applyInput(path);
      })
      .then((handler) => {
        unlisten = handler;
      });
    return () => unlisten?.();
  }, []);

  async function startAnalysis(path: string, output: string) {
    if (isMidi(path)) return;
    setNotice("正在分析当前范围");
    const directory = await join(output, "analysis");
    setAnalysisRunning(true);
    setAnalysisStage("validating");
    setAnalysisFraction(0);
    try {
      await invoke("start_analysis", {
        request: {
          input: path,
          output: directory,
          audio_track: jobRequestRef.current.audio_track,
          start_us: jobRequestRef.current.start_seconds
            ? Math.round(jobRequestRef.current.start_seconds * 1_000_000)
            : null,
          end_us: jobRequestRef.current.end_seconds
            ? Math.round(jobRequestRef.current.end_seconds * 1_000_000)
            : null,
          fft_size: 2048,
          hop_size: 512,
          window: "hann",
          spectral: true,
          worker_path: jobRequestRef.current.worker_path,
        },
      });
    } catch (reason) {
      setAnalysisRunning(false);
      setError(String(reason));
    }
  }

  async function loadAnalysisViews(directory: string) {
    try {
      const manifest = await invoke<AnalysisManifest>("analysis_manifest", { directory });
      setAnalysisManifest(manifest);
      const level = Math.min(1, manifest.waveform.levels.length - 1);
      const [waveform, spectrum] = await Promise.all([
        invoke<WaveformPayload>("analysis_waveform", { directory, level }),
        manifest.spectral
          ? invoke<SpectrumFrame>("analysis_spectrum", { directory, frame: 0 })
          : Promise.resolve(null),
      ]);
      setAnalysisWaveform(waveform);
      setAnalysisSpectrum(spectrum);
      if (manifest.spectral) {
        const image = await invoke<SpectrogramImage>("analysis_spectrogram_image", {
          directory,
          width: 1200,
          height: 256,
        });
        setAnalysisSpectrogram(image);
      }
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function applyInput(path: string, analyze = true) {
    const operation = operationForPath(path);
    const output = request.output || (await defaultOutputFor(path));
    setRecentInputs((current) => addRecentPath(current, path));
    setRequest((current) => ({
      ...current,
      input: path,
      output,
      operation,
      timing: operation === "convert_midi" ? "preserve" : current.timing,
    }));
    if (analyze && operation !== "convert_midi") await startAnalysis(path, output);
  }

  async function chooseInput() {
    const selected = await openDialog({ multiple: false, directory: false, filters: MEDIA_FILTERS });
    if (typeof selected === "string") await applyInput(selected);
  }

  async function chooseOutput() {
    const selected = await openDialog({
      multiple: false,
      directory: true,
      title: "选择输出目录",
    });
    if (typeof selected === "string") {
      setRecentOutputs((current) => addRecentPath(current, selected));
      setRequest((current) => ({ ...current, output: selected }));
    }
  }

  async function openResult() {
    const selected = await openDialog({
      multiple: false,
      directory: true,
      title: "打开既有结果目录",
    });
    if (typeof selected !== "string") return;
    try {
      const opened = await invoke<ReportDocument>("read_report", { resultDir: selected });
      setReport(opened);
      setRecentOutputs((current) => addRecentPath(current, selected));
      setResult({
        job_id: "opened-result",
        result: {
          output_dir: selected,
          report_path: "report.json",
          artifacts: opened.artifacts ?? [],
        },
      });
      setNotice("既有结果已打开");
      setError(null);
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function startJob(next = request) {
    setError(null);
    setWarnings([]);
    setResult(null);
    setReport(null);
    setRunning(true);
    setStage("validating");
    setFraction(0);
    setNotice("正在启动 worker");
    try {
      await invoke("start_job", { request: next });
    } catch (reason) {
      setRunning(false);
      setStage("failed");
      setError(String(reason));
    }
  }

  async function cancelJob() {
    await invoke("cancel_job");
    setNotice("正在取消");
  }

  async function createProject() {
    const selected = await saveDialog({
      title: "创建 GenshinLyreTranscriber 工程",
      defaultPath: request.input ? `${request.input.split(/[/\\]/).pop()}.gltproj` : "project.gltproj",
      filters: [{ name: "GLT Project", extensions: ["gltproj"] }],
    });
    if (typeof selected !== "string") return;
    const path = selected.endsWith(".gltproj") ? selected : `${selected}.gltproj`;
    const name = path.split(/[/\\]/).pop()?.replace(/\.gltproj$/i, "") || "project";
    try {
      const document = await invoke<ProjectDocument>("project_create", {
        path,
        name,
        source: request.input || null,
      });
      setProject(document);
      setNotice("工程已创建");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function openProject() {
    const selected = await openDialog({
      multiple: false,
      directory: false,
      title: "打开 GLT 工程",
      filters: [{ name: "GLT Project", extensions: ["gltproj"] }],
    });
    if (typeof selected !== "string") return;
    try {
      const document = await invoke<ProjectDocument>("project_open", { path: selected });
      setProject(document);
      if (document.source?.path) {
        await applyInput(document.source.path);
      }
      setNotice("工程已打开");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function saveProject() {
    try {
      const document = await invoke<ProjectDocument>("project_save", { name: null });
      setProject(document);
      setNotice("工程已保存");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function closeProject() {
    await invoke("project_close");
    setProject(null);
    setNotice("工程已关闭");
  }

  async function installSeparator() {
    const archive = await openDialog({
      multiple: false,
      directory: false,
      title: "选择分离组件 ZIP",
      filters: [{ name: "Separator component", extensions: ["zip"] }],
    });
    if (typeof archive !== "string" || !separatorDirectory) return;
    try {
      const status = await invoke<SeparatorComponentStatus>("separator_component_install", {
        archive,
        target: separatorDirectory,
        overwrite: true,
      });
      setSeparatorStatus(status);
      setNotice("分离组件已安装");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function uninstallSeparator() {
    if (!separatorDirectory) return;
    try {
      await invoke("separator_component_uninstall", { target: separatorDirectory });
      setSeparatorStatus(
        await invoke<SeparatorComponentStatus>("separator_component_status", {
          directory: separatorDirectory,
        }),
      );
      setNotice("分离组件已卸载");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function switchAbSource(sourceId: string) {
    const option = abOptions.find((entry) => entry.id === sourceId);
    if (!option?.path) {
      setPlaybackError(`没有可播放的${option?.label ?? "音频"}来源`);
      return;
    }
    try {
      const status = await invoke<PlaybackStatus>("playback_status");
      const position = status.available ? status.position_us : positionUs;
      await invoke("play_ab_source", {
        path: option.path,
        volume,
        positionUs: position,
      });
      setAbSource(sourceId);
      setPlayback(sourceId === "original" ? "playing-source" : "playing");
      setPlaybackError(null);
      setError(null);
    } catch (reason) {
      setPlayback("error");
      setPlaybackError(String(reason));
    }
  }

  async function playPreview() {
    await switchAbSource("preview");
  }

  async function playSource() {
    await switchAbSource("original");
  }

  async function seekAnalysis(position: number) {
    setPositionUs(position);
    try {
      await invoke("seek_playback", { positionUs: position });
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function pausePreview() {
    try {
      await invoke("pause_preview");
      setPlayback("paused");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function stopPreview() {
    try {
      await invoke("stop_preview");
      setPlayback("idle");
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function updateVolume(next: number) {
    setVolume(next);
    try {
      await invoke("set_preview_volume", { volume: next });
    } catch {
      // Volume is remembered even when no preview is loaded.
    }
  }

  async function applyPreset(preset: FilterPreset) {
    if (!result) return;
    const output = await invoke<string>("next_filter_output", {
      source: result.result.output_dir,
    });
    const next: JobRequest = {
      ...jobRequestRef.current,
      input: result.result.output_dir,
      output,
      operation: "refilter",
      filter: null,
      filter_preset: preset,
      preview_wav: true,
      overwrite: false,
    };
    setRequest(next);
    await startJob(next);
  }

  async function applyManualFilter() {
    if (!result) return;
    const filter = rulesToFilter(filterRules);
    if (!filter) {
      setError("请至少填写一条有效筛选范围");
      return;
    }
    const output = await invoke<string>("next_filter_output", {
      source: result.result.output_dir,
    });
    const next: JobRequest = {
      ...jobRequestRef.current,
      input: result.result.output_dir,
      output,
      operation: "refilter",
      filter,
      filter_preset: null,
      preview_wav: true,
      overwrite: false,
    };
    setRequest(next);
    setFilterOpen(false);
    await startJob(next);
  }

  async function recordRevision(kind: string, path: string, parentId: string | null) {
    try {
      const document = await invoke<ProjectDocument>("project_add_revision_path", {
        kind,
        absolutePath: path,
        parentId,
      });
      setProject(document);
    } catch {
      // No open project, or the output is outside the project directory.
    }
  }

  async function startSeparation() {
    const model = separatorStatus?.models[0];
    const output = await join(result?.result.output_dir ?? request.output, "stems");
    if (!request.input || !separatorDirectory || !model) {
      setError("请先选择源文件并安装至少一个分离模型");
      return;
    }
    setSeparationRunning(true);
    setSeparationStage("validating");
    setSeparationFraction(0);
    try {
      await invoke("start_separation", {
        request: {
          component: separatorDirectory,
          input: request.input,
          output,
          model: model.id,
          workerPath: request.worker_path,
        },
      });
    } catch (reason) {
      setSeparationRunning(false);
      setError(String(reason));
    }
  }

  function updateStemRoute(role: string, patch: Partial<StemRouteControl>) {
    setStemRoutes((current) => ({
      ...current,
      [role]: { ...(current[role] ?? defaultStemRoute(role)), ...patch },
    }));
    setRoutingMode("custom");
  }

  function applyDesktopPreset(preset: (typeof BUILTIN_PRESETS)[number] | (typeof customPresets)[number]) {
    setRequest((current) => ({ ...current, ...preset.values }));
    setNotice(`已应用预设：${preset.name}`);
  }

  function saveDesktopPreset() {
    const name = nextCustomPresetName(customPresets);
    const preset = {
      id: `custom-${Date.now().toString(36)}`,
      name,
      builtin: false as const,
      values: presetFromRequest(request),
    };
    setCustomPresets((current) => [...current, preset]);
    setNotice(`已保存预设：${name}`);
  }

  function tapTempo() {
    const now = globalThis.performance.now();
    const recent = recentTapTimes(tapTimesRef.current, now);
    tapTimesRef.current = recent;
    setTapCount(recent.length);
    const bpm = estimateBpm(recent);
    if (bpm === null) {
      setNotice(recent.length < 2 ? "TAP：继续跟随节拍点击" : "TAP：节拍不稳定，请重新点击");
      return;
    }
    setRequest((current) => ({ ...current, bpm }));
    setNotice(`TAP 检测 BPM：${bpm.toFixed(1)}`);
  }

  async function startRouting() {
    if (!stemSetPath) {
      setError("请先完成 stem 分离");
      return;
    }
    const output = await join(
      result?.result.output_dir ?? request.output,
      "routing",
      routingMode,
    );
    const customPlan =
      routingMode === "custom" && stemSet
        ? buildCustomRoutingPlan(
            stemSet.stems.map((stem) => stem.role),
            stemRoutes,
            request.max_voices,
          )
        : null;
    setRoutingRunning(true);
    try {
      await invoke("start_routing", {
        request: {
          stemSet: stemSetPath,
          output,
          mode: routingMode,
          maxVoices: request.max_voices,
          plan: customPlan,
          workerPath: request.worker_path,
        },
      });
    } catch (reason) {
      setRoutingRunning(false);
      setError(String(reason));
    }
  }

  async function applyEditRevision() {
    if (!result || !performance) return;
    setRunning(true);
    setEditApplying(true);
    setError(null);
    setStage("validating");
    try {
      const output = await invoke<string>("next_edit_output", {
        source: result.result.output_dir,
      });
      pendingRevisionRef.current = {
        kind: "performance-edit",
        path: output,
        parentId: performance.revision.id,
      };
      await invoke("edit_export", {
        sourceResultDir: result.result.output_dir,
        output,
        performance,
        previewWav: true,
        title: null,
        workerPath: request.worker_path,
      });
      setNotice(`正在导出 ${output.split(/[/\\]/).pop()}`);
    } catch (reason) {
      setRunning(false);
      setEditApplying(false);
      setError(String(reason));
    }
  }

  function updateRule(index: number, key: keyof DraftFilterRule, value: string | boolean) {
    setFilterRules((current) =>
      current.map((rule, ruleIndex) => (ruleIndex === index ? { ...rule, [key]: value } : rule)),
    );
  }

  function updateRuleRange(
    index: number,
    minimumKey: NumericDraftFilterKey,
    maximumKey: NumericDraftFilterKey,
    lower: number,
    upper: number,
    step: number,
  ) {
    const format = (value: number) => (step < 1 ? value.toFixed(2) : String(Math.round(value)));
    setFilterRules((current) =>
      current.map((rule, ruleIndex) =>
        ruleIndex === index
          ? { ...rule, [minimumKey]: format(lower), [maximumKey]: format(upper) }
          : rule,
      ),
    );
  }

  const operationLabel = request.operation === "convert_midi" ? "MIDI" : "音频 / 视频";

  const reportTempo =
    typeof report?.parameters?.bpm === "number" ? report.parameters.bpm : null;
  const tempoLabel = request.bpm ?? reportTempo;
  const sourceName = request.input
    ? request.input.split(/[/\\]/).pop() || request.input
    : "未载入素材";

  return (
    <div className="app-shell">
      <header className="daw-menubar">
        <div className="daw-app-mark">
          <span>GLT</span>
          <strong>Lyre Studio</strong>
        </div>
        <nav className="daw-menus" aria-label="应用菜单">
          <details className="daw-menu">
            <summary>文件</summary>
            <div className="daw-menu-popover">
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void createProject(); }}>新建工程</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void openProject(); }}>打开工程</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void chooseInput(); }}>载入素材</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void openResult(); }}>打开结果目录</button>
            </div>
          </details>
          <details className="daw-menu">
            <summary>编辑</summary>
            <div className="daw-menu-popover">
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setRequest((current) => ({ ...current, start_seconds: null, end_seconds: null })); setNotice("已清除波形选区"); }}>清除波形选区</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setWarnings([]); setError(null); setNotice("已清除提示信息"); }}>清除提示信息</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setFilterRules([{ ...EMPTY_RULE }]); setNotice("已重置筛选规则"); }}>重置筛选规则</button>
            </div>
          </details>
          <details className="daw-menu">
            <summary>操作</summary>
            <div className="daw-menu-popover">
              <button
                disabled={!request.input || !request.output}
                onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void startAnalysis(request.input, request.output); }}
              >
                分析当前范围
              </button>
              <button
                disabled={!request.input || !request.output}
                onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void startJob({ ...request }); }}
              >
                转录当前范围
              </button>
              <button
                disabled={!request.input || separationRunning || !separatorStatus?.installed}
                onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); void startSeparation(); }}
              >
                分离为四轨
              </button>
            </div>
          </details>
          <details className="daw-menu">
            <summary>视图</summary>
            <div className="daw-menu-popover">
              {[
                ["输入与编曲", "#source"],
                ["音质检查器", "#tuning"],
                ["节奏与片段", "#timing"],
                ["结果与钢琴卷帘", "#result"],
              ].map(([label, target]) => (
                <button
                  key={target}
                  onClick={(event) => {
                    event.currentTarget.closest("details")?.removeAttribute("open");
                    document.querySelector(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
                    setNotice(`已定位：${label}`);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </details>
          <details className="daw-menu">
            <summary>选项</summary>
            <div className="daw-menu-popover">
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setRequest((current) => ({ ...current, preview_wav: !current.preview_wav })); setNotice(request.preview_wav ? "已关闭预览 WAV" : "已开启预览 WAV"); }}>
                {request.preview_wav ? "关闭预览 WAV" : "开启预览 WAV"}
              </button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setAdvancedOpen((value) => !value); setNotice(advancedOpen ? "已收起高级参数" : "已展开高级参数"); }}>
                {advancedOpen ? "收起高级参数" : "展开高级参数"}
              </button>
            </div>
          </details>
          <details className="daw-menu">
            <summary>帮助</summary>
            <div className="daw-menu-popover">
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setNotice("波形拖动可选择区间；右侧起止秒可精确输入"); }}>选区操作提示</button>
              <button onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); setNotice("GLT Lyre Studio · local-only desktop workspace"); }}>关于本机版本</button>
            </div>
          </details>
        </nav>
        <div className="daw-view-tab">
          <span>ARRANGEMENT</span>
          <strong>{project?.name ?? "Untitled Session"}</strong>
        </div>
        <div className="daw-menubar-meta">
          <span className={doctor ? "online" : ""}>
            <i />
            {doctor ? "ENGINE READY" : "ENGINE CHECK"}
          </span>
          <span>{doctor ? `MODEL ${doctor.model_version}` : "NO WORKER"}</span>
        </div>
      </header>

      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">GLT</div>
          <div>
            <strong>Lyre Studio</strong>
            <span>Professional Workspace</span>
          </div>
        </div>

        <div className="browser-heading">
          <span>BROWSER</span>
          <strong>工作站</strong>
        </div>

        <nav>
          <a href="#source" className="nav-item active">
            <span>01</span> 输入与编曲
          </a>
          <a href="#tuning" className="nav-item">
            <span>02</span> 音质检查器
          </a>
          <a href="#timing" className="nav-item">
            <span>03</span> 节奏与移调
          </a>
          <a href="#result" className="nav-item">
            <span>04</span> 结果与钢琴卷帘
          </a>
        </nav>

        <div className="worker-card">
          <span className={`status-dot ${doctor ? "online" : "offline"}`} />
          <div>
            <strong>{doctor ? "Worker 在线" : "Worker 检查中"}</strong>
            <small>{doctor ? `模型 ${doctor.model_version}` : notice}</small>
          </div>
        </div>

        <div className="project-card">
          <span>PROJECT FILE</span>
          <strong>{project?.name ?? "未命名工程"}</strong>
          <small>{project?.source?.path ?? "尚未绑定源文件"}</small>
          <div className="project-actions">
            {project ? (
              <>
                <button onClick={() => void saveProject()}>保存</button>
                <button onClick={() => void closeProject()}>关闭</button>
              </>
            ) : (
              <>
                <button onClick={() => void createProject()}>新建</button>
                <button onClick={() => void openProject()}>打开</button>
              </>
            )}
          </div>
        </div>

        <div className="separator-card">
          <span>SEPARATOR COMPONENT</span>
          <strong>
            {separatorStatus?.installed
              ? `Demucs ${separatorStatus.component_version}`
              : "未安装 Demucs 组件"}
          </strong>
          <small>
            {separatorStatus?.installed
              ? separatorStatus.models.map((model) => model.id).join(" / ")
              : "基础包不包含 torch；按需安装本地 ZIP"}
          </small>
          <div className="project-actions">
            <button onClick={() => void installSeparator()}>
              {separatorStatus?.installed ? "升级" : "安装"}
            </button>
            {separatorStatus?.installed && (
              <button onClick={() => void uninstallSeparator()}>卸载</button>
            )}
          </div>
        </div>
      </aside>

      <main className="workspace">
        <header className="hero">
          <div className="transport-strip" aria-label="播放传输控制">
            <button
              className="transport-button"
              onClick={() => void seekAnalysis(0)}
              disabled={!analysisManifest}
              title="回到开头"
            >
              |◀
            </button>
            <button className="transport-button" onClick={() => void stopPreview()} title="停止">
              ■
            </button>
            <button
              className="transport-button play"
              onClick={() => void playSource()}
              disabled={!request.input}
              title="播放原音"
            >
              ▶
            </button>
            <button
              className="transport-button"
              onClick={() => void pausePreview()}
              disabled={!hasActiveAbSource}
              title="暂停"
            >
              Ⅱ
            </button>
          </div>

          <div className="transport-readout">
            <span>TIME</span>
            <strong>{formatClock(positionUs)}</strong>
          </div>

          <div className="transport-readout compact">
            <span>TEMPO</span>
            <strong>{tempoLabel ? `${Number(tempoLabel).toFixed(3)} BPM` : "AUTO BPM"}</strong>
          </div>

          <div className="transport-readout compact">
            <span>GRID</span>
            <strong>{request.timing.toUpperCase()}</strong>
          </div>

          <div className="toolbar-separator" />

          <div className="tool-cluster">
            <button className="ghost-button" onClick={chooseInput}>
              载入素材
            </button>
            <button className="ghost-button" onClick={() => void openResult()}>
              打开结果
            </button>
          </div>

          <div className="toolbar-separator" />

          <div className="tool-context">
            <span>ACTIVE SOURCE</span>
            <strong title={request.input}>{sourceName}</strong>
          </div>

          <div className={`run-state ${running ? "running" : ""}`}>
            <span />
            {running ? stage : notice}
          </div>

          <div className="toolbar-progress" aria-hidden="true">
            <i style={{ width: `${Math.max(0, Math.min(100, (fraction ?? (running ? 0 : 1)) * 100))}%` }} />
          </div>
        </header>

        <section id="source" className="panel source-panel">
          <div className="panel-heading">
            <div>
              <span className="section-number">01</span>
              <h2>输入与输出</h2>
            </div>
            <button className="ghost-button" onClick={chooseInput}>
              选择文件
            </button>
          </div>

          <div className="arrange-ruler" aria-hidden="true">
            {["1", "2", "3", "4", "5", "6", "7", "8"].map((beat) => (
              <span key={beat}>{beat}</span>
            ))}
          </div>

          <div className="lane-header">
            <span>TRACK 01</span>
            <strong>TRANSCRIPTION INPUT</strong>
            <i className={request.input ? "loaded" : ""}>{request.input ? "LOADED" : "EMPTY"}</i>
          </div>

          <button className="dropzone" onClick={chooseInput} onDragOver={(event) => event.preventDefault()}>
            <span className="drop-icon">↓</span>
            <strong>{request.input ? request.input.split(/[/\\]/).pop() : "拖入音频、视频或 MIDI"}</strong>
            <small>{request.input || "支持 FLAC、WAV、MP3、MP4、MKV、MID、MIDI 等"}</small>
          </button>

          <div className="field-row">
            <label className="field grow">
              <span>输入路径</span>
              <input
                value={request.input}
                onChange={(event) => {
                  const path = event.target.value;
                  const operation = operationForPath(path);
                  setRequest((current) => ({
                    ...current,
                    input: path,
                    operation,
                    timing: operation === "convert_midi" ? "preserve" : current.timing,
                  }));
                }}
                placeholder="选择或拖入文件"
              />
            </label>
            <label className="field grow">
              <span>输出目录</span>
              <div className="joined-input">
                <input
                  value={request.output}
                  onChange={(event) =>
                    setRequest((current) => ({ ...current, output: event.target.value }))
                  }
                  placeholder="结果目录"
                />
                <button
                  type="button"
                  disabled={!request.input}
                  onClick={() =>
                    setRequest((current) => ({
                      ...current,
                      output: parentPath(current.input),
                    }))
                  }
                >
                  同目录
                </button>
                <button type="button" onClick={() => void chooseOutput()}>
                  浏览
                </button>
              </div>
            </label>
          </div>
          <div className="hint-row">
            <span className="pill">{operationLabel}</span>
            <span>输入 MIDI 会自动切换到 preserve，避免覆盖已有 tempo map。</span>
          </div>

          {recentInputs.length > 0 && (
            <div className="recent-path-strip">
              <span>最近素材</span>
              <div>
                {recentInputs.slice(0, 5).map((path) => (
                  <button type="button" key={path} title={path} onClick={() => void applyInput(path)}>
                    {path.split(/[/\\]/).pop() || path}
                  </button>
                ))}
              </div>
              <button type="button" onClick={() => setRecentInputs([])}>清空</button>
            </div>
          )}

          {recentOutputs.length > 0 && (
            <div className="recent-path-strip">
              <span>最近输出</span>
              <div>
                {recentOutputs.slice(0, 5).map((path) => (
                  <button
                    type="button"
                    key={path}
                    title={path}
                    onClick={() => setRequest((current) => ({ ...current, output: path }))}
                  >
                    {path.split(/[/\\]/).pop() || path}
                  </button>
                ))}
              </div>
              <button type="button" onClick={() => setRecentOutputs([])}>清空</button>
            </div>
          )}
        </section>

        {analysisManifest && analysisWaveform && (
          <section className="panel analysis-panel">
            <div className="panel-heading">
              <div>
                <span className="section-number">ANALYSIS</span>
                <h2>音频分析</h2>
              </div>
              <div className="button-row">
                <button className="primary-button" onClick={() => void playSource()}>
                  {playback === "playing-source" ? "重新播放原音" : "播放原音"}
                </button>
                <button className="ghost-button" onClick={() => void pausePreview()}>
                  暂停
                </button>
                <button className="ghost-button" onClick={() => void stopPreview()}>
                  停止
                </button>
              </div>
            </div>
            <div className="analysis-status">
              <span>{analysisRunning ? analysisStage : "分析就绪"}</span>
              <strong>
                {analysisFraction === null ? "—" : `${Math.round(analysisFraction * 100)}%`}
              </strong>
            </div>
            <AnalysisView
              manifest={analysisManifest}
              waveform={analysisWaveform}
              spectrogram={analysisSpectrogram}
              spectrum={analysisSpectrum}
              positionUs={positionUs}
              selectionStartUs={
                request.start_seconds === null
                  ? null
                  : Math.round(request.start_seconds * 1_000_000)
              }
              selectionEndUs={
                request.end_seconds === null
                  ? null
                  : Math.round(request.end_seconds * 1_000_000)
              }
              onSeek={(position) => void seekAnalysis(position)}
              onSelectionChange={(startUs, endUs) =>
                setRequest((current) => ({
                  ...current,
                  start_seconds: startUs === null ? null : startUs / 1_000_000,
                  end_seconds: endUs === null ? null : endUs / 1_000_000,
                }))
              }
              onAnalyzeSelection={() => void startAnalysis(request.input, request.output)}
              onTranscribeSelection={() => void startJob({ ...request })}
              loopEnabled={loopEnabled}
              onToggleLoop={() => {
                setLoopEnabled((value) => !value);
                setNotice(loopEnabled ? "已关闭选区循环" : "已开启选区循环");
              }}
            />
          </section>
        )}

        {request.input && (
          <section className="panel stem-panel">
            <div className="panel-heading">
              <div>
                <span className="section-number">STEM</span>
                <h2>分离与路由</h2>
              </div>
              <button
                className="primary-button"
                onClick={() => void startSeparation()}
                disabled={
                  separationRunning ||
                  !separatorStatus?.installed ||
                  separatorStatus.models.length === 0
                }
              >
                {separationRunning ? "分离中" : "分离为四轨"}
              </button>
            </div>
            <div className="stem-status">
              <div>
                <span>运行组件</span>
                <strong>
                  {separatorStatus?.installed
                    ? `Demucs ${separatorStatus.component_version}`
                    : "未安装"}
                </strong>
              </div>
              <div>
                <span>分离状态</span>
                <strong>
                  {separationRunning
                    ? `${separationStage} ${separationFraction === null ? "" : `${Math.round(separationFraction * 100)}%`}`
                    : stemSet
                      ? "四轨与 instrumental 就绪"
                      : "等待分离"}
                </strong>
              </div>
            </div>
            {stemSet && (
              <>
                <div className="stem-audition-grid">
                  {stemSet.stems.map((stem) => {
                    const sourceId = `stem-${stem.role}`;
                    const option = abOptions.find((entry) => entry.id === sourceId);
                    const control = stemRoutes[stem.role] ?? defaultStemRoute(stem.role);
                    return (
                      <div
                        className={`stem-channel-card ${abSource === sourceId ? "active" : ""}`}
                        key={stem.role}
                      >
                        <button
                          type="button"
                          className="stem-play-button"
                          aria-pressed={abSource === sourceId}
                          disabled={!option?.path}
                          onClick={() => void switchAbSource(sourceId)}
                        >
                          <span>{stemLabel(stem.role)}</span>
                          <strong>{(stem.duration_us / 1_000_000).toFixed(1)}s</strong>
                          <small>{stem.sample_rate / 1000} kHz · 试听</small>
                        </button>
                        <div className="stem-toggle-row">
                          <button
                            type="button"
                            className={control.muted ? "active mute" : ""}
                            aria-pressed={control.muted}
                            onClick={() => updateStemRoute(stem.role, { muted: !control.muted })}
                          >
                            M
                          </button>
                          <button
                            type="button"
                            className={control.solo ? "active solo" : ""}
                            aria-pressed={control.solo}
                            onClick={() => updateStemRoute(stem.role, { solo: !control.solo })}
                          >
                            S
                          </button>
                        </div>
                        <label className="stem-gain">
                          <span>Gain {control.gainDb.toFixed(1)} dB</span>
                          <input
                            type="range"
                            min="-24"
                            max="12"
                            step="1"
                            value={control.gainDb}
                            onChange={(event) =>
                              updateStemRoute(stem.role, { gainDb: Number(event.target.value) })
                            }
                          />
                        </label>
                        <div className="stem-target-row">
                          {STEM_TARGETS.map((target) => (
                            <button
                              type="button"
                              key={target.value}
                              title={target.title}
                              className={control.target === target.value ? "active" : ""}
                              onClick={() =>
                                updateStemRoute(stem.role, { target: target.value })
                              }
                            >
                              {target.label}
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="routing-mode-grid">
                  {[
                    { value: "solo", title: "SOLO", detail: "主旋律单声部" },
                    { value: "melody_chords", title: "MELODY + CHORDS", detail: "旋律与和声" },
                    { value: "two_voice", title: "TWO VOICE", detail: "旋律 + 低音" },
                    { value: "full", title: "FULL", detail: "保留完整织体" },
                    { value: "custom", title: "CUSTOM", detail: "手动 M/S/音量/角色" },
                  ].map((mode) => (
                    <button
                      type="button"
                      key={mode.value}
                      className={routingMode === mode.value ? "active" : ""}
                      onClick={() => setRoutingMode(mode.value)}
                    >
                      <strong>{mode.title}</strong>
                      <small>{mode.detail}</small>
                    </button>
                  ))}
                </div>
                <div className="routing-row">
                  <button
                    className="primary-button"
                    onClick={() => void startRouting()}
                    disabled={routingRunning}
                  >
                    {routingRunning ? "路由中" : "生成所选路由试听"}
                  </button>
                  {routedAudioPath && <span className="pill">A/B 已加入“路由”来源</span>}
                </div>
              </>
            )}
          </section>
        )}

        <section id="tuning" className="panel">
          <div className="panel-heading">
            <div>
              <span className="section-number">02</span>
              <h2>音质与演奏</h2>
            </div>
          </div>

          <div className="desktop-preset-bar">
            <span>参数预设</span>
            <div>
              {[...BUILTIN_PRESETS, ...customPresets].map((preset) => (
                <div className="desktop-preset-card" key={preset.id}>
                  <button
                    type="button"
                    title={preset.builtin ? "内置预设" : "自定义预设"}
                    onClick={() => applyDesktopPreset(preset)}
                  >
                    {preset.name}
                  </button>
                  {!preset.builtin && (
                    <button
                      type="button"
                      className="remove"
                      title="删除预设"
                      onClick={() =>
                        setCustomPresets((current) =>
                          current.filter((candidate) => candidate.id !== preset.id),
                        )
                      }
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
              <button type="button" className="save" onClick={saveDesktopPreset}>
                + 保存当前
              </button>
            </div>
          </div>

          <SegmentedControl
            label="清理档位"
            value={request.cleaning_profile}
            options={[
              { value: "auto", label: "AUTO", hint: "自动平衡" },
              { value: "solo", label: "SOLO", hint: "独奏优先" },
              { value: "mix", label: "MIX", hint: "混音降噪" },
              { value: "strict", label: "STRICT", hint: "严格筛选" },
            ]}
            onChange={(value) =>
              setRequest((current) => ({ ...current, cleaning_profile: value }))
            }
          />
          <SegmentedControl
            label="可演奏性编排"
            value={request.arrangement}
            options={[
              { value: "balanced", label: "BALANCED", hint: "自动二声部" },
              { value: "off", label: "RAW", hint: "保留全部候选" },
            ]}
            onChange={(value) =>
              setRequest((current) => ({ ...current, arrangement: value }))
            }
          />
          <div className="parameter-grid">
            <ParameterSlider
              label="最低置信度"
              minimum={0}
              maximum={1}
              step={0.01}
              value={request.min_confidence}
              fallback={0.2}
              precision={2}
              unsetLabel="跟随档位"
              onChange={(value) =>
                setRequest((current) => ({ ...current, min_confidence: value }))
              }
            />
            <ParameterSlider
              label="最短时值"
              minimum={0}
              maximum={1000}
              step={10}
              value={request.min_duration_ms}
              fallback={50}
              unit="ms"
              unsetLabel="跟随档位"
              onChange={(value) =>
                setRequest((current) => ({ ...current, min_duration_ms: value }))
              }
            />
          </div>
          <button className="text-button" onClick={() => setAdvancedOpen((value) => !value)}>
            {advancedOpen ? "收起高级清理" : "展开高级清理"}
          </button>
          {advancedOpen && (
            <div className="parameter-grid advanced">
              <ParameterSlider
                label="重触发间隔"
                minimum={0}
                maximum={300}
                step={5}
                value={request.retrigger_gap_ms}
                fallback={30}
                unit="ms"
                unsetLabel="默认 30 ms"
                onChange={(value) =>
                  setRequest((current) => ({ ...current, retrigger_gap_ms: value }))
                }
              />
              <ParameterSlider
                label="近同时窗口"
                minimum={20}
                maximum={500}
                step={10}
                value={request.onset_window_ms}
                fallback={150}
                unit="ms"
                onChange={(value) =>
                  setRequest((current) => ({ ...current, onset_window_ms: value ?? 150 }))
                }
              />
              <ParameterSlider
                label="最大同时声部"
                minimum={1}
                maximum={6}
                step={1}
                value={request.max_voices}
                fallback={2}
                onChange={(value) =>
                  setRequest((current) => ({ ...current, max_voices: value ?? 2 }))
                }
              />
            </div>
          )}
        </section>

        <section id="timing" className="panel">
          <div className="panel-heading">
            <div>
              <span className="section-number">03</span>
              <h2>节奏、移调与片段</h2>
            </div>
          </div>
          <SegmentedControl
            label="时序模式"
            value={request.timing}
            options={[
              { value: "auto", label: "AUTO", hint: "自动分析" },
              { value: "preserve", label: "RAW", hint: "保留原时序" },
              { value: "straight", label: "1/16", hint: "十六分直拍" },
              { value: "triplet", label: "TRIPLET", hint: "三连音" },
            ]}
            onChange={(value) => setRequest((current) => ({ ...current, timing: value }))}
          />
          <div className="parameter-grid">
            <div className="parameter-with-action">
              <ParameterSlider
                label="BPM 覆盖"
                minimum={40}
                maximum={240}
                step={0.1}
                value={request.bpm}
                fallback={tempoLabel ?? 120}
                precision={1}
                unsetLabel="自动 BPM"
                onChange={(value) => setRequest((current) => ({ ...current, bpm: value }))}
              />
              <button type="button" className="tap-tempo-button" onClick={tapTempo}>
                <strong>TAP</strong>
                <small>{tapCount ? `${tapCount} 次` : "跟随节拍点击"}</small>
              </button>
            </div>
            <ParameterSlider
              label="整体移调"
              minimum={-24}
              maximum={24}
              step={1}
              value={request.transpose === "auto" ? null : request.transpose}
              fallback={0}
              unit="st"
              unsetLabel="自动移调"
              onChange={(value) =>
                setRequest((current) => ({ ...current, transpose: value ?? "auto" }))
              }
            />
          </div>
          <div className="segmented-field transpose-shortcuts">
            <span>移调快捷</span>
            <div className="segmented-control">
              {(["auto", -12, -1, 0, 1, 12] as const).map((value) => (
                <button
                  type="button"
                  key={String(value)}
                  className={String(request.transpose) === String(value) ? "active" : ""}
                  onClick={() =>
                    setRequest((current) => ({
                      ...current,
                      transpose: value,
                    }))
                  }
                >
                  <strong>{value === "auto" ? "AUTO" : value > 0 ? `+${value}` : value}</strong>
                </button>
              ))}
            </div>
          </div>
          <div className="audio-track-picker">
            <div className="audio-track-heading">
              <span>音轨选择</span>
              <small>
                {mediaProbeRunning
                  ? "正在探测媒体…"
                  : mediaProbe
                    ? `${mediaProbe.format_name} · ${(mediaProbe.duration_us / 1_000_000).toFixed(1)}s`
                    : "选择素材后自动显示音轨"}
              </small>
            </div>
            <div className="audio-track-grid">
              <button
                type="button"
                className={request.audio_track === null ? "active" : ""}
                onClick={() => setRequest((current) => ({ ...current, audio_track: null }))}
              >
                <strong>DEFAULT</strong>
                <small>默认音轨</small>
              </button>
              {(mediaProbe?.audio_streams ?? []).map((stream) => (
                <button
                  type="button"
                  key={stream.position}
                  className={request.audio_track === stream.position ? "active" : ""}
                  onClick={() =>
                    setRequest((current) => ({ ...current, audio_track: stream.position }))
                  }
                >
                  <strong>
                    #{stream.position}
                    {stream.is_default ? " ★" : ""}
                  </strong>
                  <small>
                    {stream.codec_name}
                    {stream.channels ? ` · ${stream.channels}ch` : ""}
                    {stream.sample_rate ? ` · ${(stream.sample_rate / 1000).toFixed(1)}k` : ""}
                  </small>
                  <small>{stream.title || stream.language || `stream ${stream.index}`}</small>
                </button>
              ))}
            </div>
            {mediaProbeError && <small className="media-probe-error">{mediaProbeError}</small>}
          </div>
          <div className="control-grid compact-row">
            <label className="field">
              <span>起点秒</span>
              <input
                type="number"
                min="0"
                step="0.1"
                value={request.start_seconds ?? ""}
                placeholder="0"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    start_seconds: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>
            <label className="field">
              <span>终点秒</span>
              <input
                type="number"
                min="0"
                step="0.1"
                value={request.end_seconds ?? ""}
                placeholder="结束"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    end_seconds: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>
          </div>
          <div className="switch-row">
            <label className="switch">
              <input
                type="checkbox"
                checked={request.preview_wav}
                onChange={(event) =>
                  setRequest((current) => ({ ...current, preview_wav: event.target.checked }))
                }
              />
              <span>生成预览 WAV</span>
            </label>
            <label className="switch">
              <input
                type="checkbox"
                checked={request.overwrite}
                onChange={(event) =>
                  setRequest((current) => ({ ...current, overwrite: event.target.checked }))
                }
              />
              <span>允许覆盖已有输出</span>
            </label>
          </div>
        </section>

        <div className="run-bar">
          <div className="progress-copy">
            <strong>{running ? stage : "准备开始"}</strong>
            <span>{fraction === null ? "进度未知" : `${Math.round(fraction * 100)}%`}</span>
          </div>
          <div className="progress-track">
            <div
              className={fraction === null && running ? "indeterminate" : ""}
              style={{ width: `${fraction === null ? 32 : fraction * 100}%` }}
            />
          </div>
          {running ? (
            <button className="danger-button" onClick={() => void cancelJob()}>
              取消任务
            </button>
          ) : (
            <button
              className="primary-button"
              disabled={!request.input || !request.output}
              onClick={() => void startJob()}
            >
              开始转换
            </button>
          )}
        </div>

        {(error || warnings.length > 0) && (
          <section className="alerts">
            {error && <div className="alert error">{error}</div>}
            {warnings.map((warning, index) => (
              <div className="alert warning" key={`${warning}-${index}`}>
                {warning}
              </div>
            ))}
          </section>
        )}

        {result && (
          <section id="result" className="panel result-panel">
            <div className="panel-heading">
              <div>
                <span className="section-number">04</span>
                <h2>结果、筛选与试听</h2>
              </div>
              <div className="button-row">
                <button className="ghost-button" onClick={() => void openResult()}>
                  打开已有结果
                </button>
                <button
                  className="ghost-button"
                  onClick={() => void openPath(result.result.output_dir)}
                >
                  打开结果目录
                </button>
              </div>
            </div>

            <div className="result-summary">
              <div className="result-path">
                <span>结果目录</span>
                <strong>{result.result.output_dir}</strong>
              </div>
              <div className="filter-actions">
                <button onClick={() => void applyPreset("auto")}>自动检测</button>
                <button onClick={() => void applyPreset("balanced")}>balanced</button>
                <button onClick={() => void applyPreset("melody")}>melody</button>
                <button onClick={() => setFilterOpen((value) => !value)}>手动筛选</button>
              </div>
            </div>

            {filterOpen && (
              <div className="filter-editor">
                <div className="filter-live-preview">
                  <div className="filter-live-copy">
                    <span>即时筛选预览</span>
                    <strong>
                      {filterPreview.total === 0
                        ? "没有候选缓存"
                        : `保留 ${filterPreview.matched} · 删除 ${filterPreview.removed} · ${Math.round((filterPreview.matched / Math.max(1, filterPreview.total)) * 100)}%`}
                    </strong>
                  </div>
                  <div className="filter-live-track">
                    <i
                      style={{
                        width: `${filterPreview.total ? (filterPreview.matched / filterPreview.total) * 100 : 0}%`,
                      }}
                    />
                  </div>
                  <small>拖动阈值即时重算；“应用横向阈值”后才生成新的不可变结果目录。</small>
                </div>
                <FilterPreviewCanvas
                  notes={candidateNotes}
                  rules={filterRules}
                  onPitchLineChange={(ruleIndex, bound, pitch) =>
                    updateRule(
                      ruleIndex,
                      bound === "min" ? "pitchMin" : "pitchMax",
                      String(pitch),
                    )
                  }
                />
                {filterRules.map((rule, index) => (
                  <div className="filter-rule" key={index}>
                    <div className="filter-rule-title">
                      <label className="switch">
                        <input
                          type="checkbox"
                          checked={rule.enabled}
                          onChange={(event) => updateRule(index, "enabled", event.target.checked)}
                        />
                        <span>
                          规则 {index + 1}
                          {filterPreview.total > 0 && ` · ${filterPreview.perRule[index] ?? 0} 命中`}
                        </span>
                      </label>
                      {filterRules.length > 1 && (
                        <button
                          className="text-button danger"
                          onClick={() =>
                            setFilterRules((current) =>
                              current.filter((_, ruleIndex) => ruleIndex !== index),
                            )
                          }
                        >
                          删除
                        </button>
                      )}
                    </div>
                    {FILTER_FIELDS.map((field) => {
                      const lower =
                        parseOptionalNumber(rule[field.minimumKey]) ?? field.lowerFallback;
                      const upper =
                        parseOptionalNumber(rule[field.maximumKey]) ?? field.upperFallback;
                      return (
                        <div className="filter-threshold" key={field.label}>
                          <FilterRangeSlider
                            label={field.label}
                            minimum={field.minimum}
                            maximum={field.maximum}
                            step={field.step}
                            lower={lower}
                            upper={upper}
                            unit={field.unit}
                            onChange={(nextLower, nextUpper) =>
                              updateRuleRange(
                                index,
                                field.minimumKey,
                                field.maximumKey,
                                nextLower,
                                nextUpper,
                                field.step,
                              )
                            }
                          />
                          <div className="range-row">
                            <span>{field.label}</span>
                            <input
                              value={rule[field.minimumKey]}
                              placeholder="min"
                              onChange={(event) =>
                                updateRule(index, field.minimumKey, event.target.value)
                              }
                            />
                            <span>—</span>
                            <input
                              value={rule[field.maximumKey]}
                              placeholder="max"
                              onChange={(event) =>
                                updateRule(index, field.maximumKey, event.target.value)
                              }
                            />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ))}
                <div className="button-row">
                  {filterRules.length < 4 && (
                    <button
                      className="ghost-button"
                      onClick={() => setFilterRules((current) => [...current, { ...EMPTY_RULE }])}
                    >
                      添加横向阈值组
                    </button>
                  )}
                  <button className="primary-button" onClick={() => void applyManualFilter()}>
                    应用横向阈值
                  </button>
                </div>
              </div>
            )}

            <div className="stats-grid">
              {counts.map(([key, value]) => (
                <div className="stat" key={key}>
                  <span>{countLabel(key)}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>

            {performance && (
              <PianoRollEditor
                key={performance.revision.id}
                document={performance}
                candidates={candidateNotes}
                positionUs={positionUs}
                applying={editApplying}
                onDocumentChange={setPerformance}
                onApply={() => void applyEditRevision()}
              />
            )}

            <div className="result-grid">
              <div className="preview-card">
                <div>
                  <span>合成试听</span>
                  <strong>{previewArtifact ? "preview.wav 已生成" : "没有 preview.wav"}</strong>
                </div>
                <div className="playback-controls">
                  {abOptions.map((option) => (
                    <button
                      key={option.id}
                      className={abSource === option.id ? "primary-button" : undefined}
                      onClick={() => void switchAbSource(option.id)}
                      disabled={!option.path}
                    >
                      {option.label}
                    </button>
                  ))}
                  <button onClick={() => void playPreview()} disabled={!previewArtifact}>
                    播放当前
                  </button>
                  <button onClick={() => void pausePreview()} disabled={!hasActiveAbSource}>
                    暂停
                  </button>
                  <button onClick={() => void stopPreview()} disabled={!hasActiveAbSource}>
                    停止
                  </button>
                </div>
                <label className="volume-control">
                  <span>音量 {Math.round(volume * 100)}%</span>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.01"
                    value={volume}
                    onChange={(event) => void updateVolume(Number(event.target.value))}
                  />
                </label>
                {playbackError && <small className="playback-error">{playbackError}</small>}
              </div>

              <div className="artifact-list">
                <h3>产物</h3>
                {result.result.artifacts.map((artifact) => (
                  <button
                    key={`${artifact.kind}-${artifact.relative_path}`}
                    onClick={() => void openPath(`${result.result.output_dir}\\${artifact.relative_path}`)}
                  >
                    <span>{artifact.kind}</span>
                    <strong>{artifact.relative_path}</strong>
                    <small>{formatBytes(artifact.size_bytes)}</small>
                  </button>
                ))}
              </div>
            </div>
          </section>
        )}
      </main>
      {toast && (
        <div className="interaction-toast" role="status" aria-live="polite">
          {toast}
        </div>
      )}
    </div>
  );
}

export default App;
