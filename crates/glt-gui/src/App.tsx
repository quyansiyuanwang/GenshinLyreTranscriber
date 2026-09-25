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
  CleaningProfile,
  DoctorInfo,
  DraftFilterRule,
  FilterPreset,
  FilterRule,
  FilterSpec,
  JobEvent,
  JobRequest,
  JobResult,
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
  Timing,
  Transpose,
  WaveformPayload,
} from "./types";
import AnalysisView from "./AnalysisView";
import FilterRangeSlider from "./FilterRangeSlider";
import PianoRollEditor from "./PianoRollEditor";

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

function parseOptionalNumber(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function hasRuleValue(rule: DraftFilterRule): boolean {
  return Object.entries(rule).some(([key, value]) => key !== "enabled" && value !== "");
}

function range(
  minimum: string,
  maximum: string,
  fallbackMin: number,
  fallbackMax: number,
): { min: number; max: number } | undefined {
  if (!minimum.trim() && !maximum.trim()) return undefined;
  return {
    min: parseOptionalNumber(minimum) ?? fallbackMin,
    max: parseOptionalNumber(maximum) ?? fallbackMax,
  };
}

function rulesToFilter(rules: DraftFilterRule[]): FilterSpec | null {
  const converted: FilterRule[] = [];
  for (const rule of rules) {
    if (!rule.enabled || !hasRuleValue(rule)) continue;
    converted.push({
      enabled: true,
      confidence: range(rule.confidenceMin, rule.confidenceMax, 0, 1),
      duration_ms: range(rule.durationMin, rule.durationMax, 0, 3_600_000),
      velocity: range(rule.velocityMin, rule.velocityMax, 1, 127),
      pitch: range(rule.pitchMin, rule.pitchMax, 0, 127),
    });
  }
  return converted.length ? { format_version: 1, rules: converted } : null;
}

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

function App() {
  const [request, setRequest] = useState<JobRequest>(DEFAULT_REQUEST);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterRules, setFilterRules] = useState<DraftFilterRule[]>([{ ...EMPTY_RULE }]);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState("待机");
  const [fraction, setFraction] = useState<number | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("准备就绪");
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
  const [routedAudioPath, setRoutedAudioPath] = useState<string | null>(null);
  const [doctor, setDoctor] = useState<DoctorInfo | null>(null);
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
  }, [analysisDirectory, analysisManifest]);

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
    setRoutingRunning(true);
    try {
      await invoke("start_routing", {
        request: {
          stemSet: stemSetPath,
          output,
          mode: routingMode,
          maxVoices: request.max_voices,
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
                <button onClick={chooseOutput}>浏览</button>
              </div>
            </label>
          </div>
          <div className="hint-row">
            <span className="pill">{operationLabel}</span>
            <span>输入 MIDI 会自动切换到 preserve，避免覆盖已有 tempo map。</span>
          </div>
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
              <div className="routing-row">
                <label className="field">
                  <span>演奏模板</span>
                  <select value={routingMode} onChange={(event) => setRoutingMode(event.target.value)}>
                    <option value="solo">Solo</option>
                    <option value="melody_chords">Melody + Chords</option>
                    <option value="two_voice">Two Voice</option>
                    <option value="full">Full</option>
                  </select>
                </label>
                <button
                  className="primary-button"
                  onClick={() => void startRouting()}
                  disabled={routingRunning}
                >
                  {routingRunning ? "路由中" : "生成路由试听"}
                </button>
                {routedAudioPath && <span className="pill">A/B 已加入“路由”来源</span>}
              </div>
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

          <div className="control-grid">
            <label className="field">
              <span>清理档位</span>
              <select
                value={request.cleaning_profile}
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    cleaning_profile: event.target.value as CleaningProfile,
                  }))
                }
              >
                <option value="auto">auto · 自动平衡</option>
                <option value="solo">solo · 独奏优先</option>
                <option value="mix">mix · 混音降噪</option>
                <option value="strict">strict · 严格筛选</option>
              </select>
            </label>

            <label className="field">
              <span>可演奏性编排</span>
              <select
                value={request.arrangement}
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    arrangement: event.target.value as "balanced" | "off",
                  }))
                }
              >
                <option value="balanced">balanced · 自动二声部</option>
                <option value="off">off · 保留全部候选</option>
              </select>
            </label>

            <label className="field">
              <span>最小置信度</span>
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={request.min_confidence ?? ""}
                placeholder="跟随档位"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    min_confidence: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>

            <label className="field">
              <span>最短时值 ms</span>
              <input
                type="number"
                min="0"
                max="60000"
                value={request.min_duration_ms ?? ""}
                placeholder="跟随档位"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    min_duration_ms: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>
          </div>
          <button className="text-button" onClick={() => setAdvancedOpen((value) => !value)}>
            {advancedOpen ? "收起高级清理" : "展开高级清理"}
          </button>
          {advancedOpen && (
            <div className="control-grid advanced">
              <label className="field">
                <span>重触发间隔 ms</span>
                <input
                  type="number"
                  min="0"
                  max="60000"
                  value={request.retrigger_gap_ms ?? ""}
                  placeholder="30"
                  onChange={(event) =>
                    setRequest((current) => ({
                      ...current,
                      retrigger_gap_ms: parseOptionalNumber(event.target.value),
                    }))
                  }
                />
              </label>
              <label className="field">
                <span>近同时窗口 ms</span>
                <input
                  type="number"
                  min="1"
                  max="1000"
                  value={request.onset_window_ms}
                  onChange={(event) =>
                    setRequest((current) => ({
                      ...current,
                      onset_window_ms: Number(event.target.value),
                    }))
                  }
                />
              </label>
              <label className="field">
                <span>最大同时声部</span>
                <input
                  type="number"
                  min="1"
                  max="21"
                  value={request.max_voices}
                  onChange={(event) =>
                    setRequest((current) => ({
                      ...current,
                      max_voices: Number(event.target.value),
                    }))
                  }
                />
              </label>
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
          <div className="control-grid">
            <label className="field">
              <span>时序模式</span>
              <select
                value={request.timing}
                onChange={(event) =>
                  setRequest((current) => ({ ...current, timing: event.target.value as Timing }))
                }
              >
                <option value="auto">auto · 自动分析</option>
                <option value="preserve">preserve · 保留原时序</option>
                <option value="straight">straight · 十六分直拍</option>
                <option value="triplet">triplet · 三连音</option>
              </select>
            </label>
            <label className="field">
              <span>BPM 覆盖</span>
              <input
                type="number"
                min="1"
                max="1000"
                value={request.bpm ?? ""}
                placeholder="自动"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    bpm: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>
            <label className="field">
              <span>整体移调</span>
              <input
                value={String(request.transpose)}
                onChange={(event) => {
                  const raw = event.target.value.trim();
                  const transpose: Transpose = raw === "auto" ? "auto" : Number(raw);
                  setRequest((current) => ({ ...current, transpose }));
                }}
                placeholder="auto 或半音数"
              />
            </label>
            <label className="field">
              <span>音轨编号</span>
              <input
                type="number"
                min="0"
                value={request.audio_track ?? ""}
                placeholder="默认音轨"
                onChange={(event) =>
                  setRequest((current) => ({
                    ...current,
                    audio_track: parseOptionalNumber(event.target.value),
                  }))
                }
              />
            </label>
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
                {filterRules.map((rule, index) => (
                  <div className="filter-rule" key={index}>
                    <div className="filter-rule-title">
                      <label className="switch">
                        <input
                          type="checkbox"
                          checked={rule.enabled}
                          onChange={(event) => updateRule(index, "enabled", event.target.checked)}
                        />
                        <span>规则 {index + 1}</span>
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
                    应用筛选
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
    </div>
  );
}

export default App;
