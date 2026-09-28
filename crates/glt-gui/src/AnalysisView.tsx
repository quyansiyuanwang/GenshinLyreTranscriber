import { useCanvasSize } from "./useCanvasSize";
import { PlaybackPlayhead, PlaybackSeconds } from "./LivePlaybackViews";
import { usePlaybackSpectrum } from "./playbackRuntime";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { formatRangeTime, normalizeRangeUs, type TimeRangeUs } from "./analysisSelection";
import type {
  AnalysisManifest,
  SpectrogramImage,
  WaveformPayload,
} from "./types";

interface AnalysisViewProps {
  manifest: AnalysisManifest;
  waveform: WaveformPayload;
  spectrogram: SpectrogramImage | null;
  onVisibilityChange: (visible: boolean) => void;
  selectionStartUs: number | null;
  selectionEndUs: number | null;
  onSeek: (positionUs: number) => void;
  onSelectionChange: (startUs: number | null, endUs: number | null) => void;
  onAnalyzeSelection: () => void;
  onTranscribeSelection: () => void;
  loopEnabled: boolean;
  onToggleLoop: () => void;
}

interface DragState {
  pointerId: number;
  startX: number;
  anchorUs: number;
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function setupCanvas(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context?.setTransform(ratio, 0, 0, ratio, 0, 0);
  return context;
}

function intensityToColor(value: number): [number, number, number] {
  const normalized = value / 255;
  if (normalized < 0.42) {
    const blend = normalized / 0.42;
    return [Math.round(18 + blend * 18), Math.round(23 + blend * 57), Math.round(29 + blend * 73)];
  }
  if (normalized < 0.78) {
    const blend = (normalized - 0.42) / 0.36;
    return [Math.round(36 + blend * 194), Math.round(80 + blend * 63), Math.round(102 + blend * 7)];
  }
  const blend = (normalized - 0.78) / 0.22;
  return [Math.round(230 + blend * 25), Math.round(143 + blend * 77), Math.round(109 + blend * 30)];
}

function pointerTime(event: React.PointerEvent<HTMLCanvasElement>, durationUs: number): number {
  const bounds = event.currentTarget.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
  return Math.round(ratio * durationUs);
}

function selectionRange(
  selectionStartUs: number | null,
  selectionEndUs: number | null,
  durationUs: number,
): TimeRangeUs | null {
  if (selectionStartUs === null || selectionEndUs === null) return null;
  const range = normalizeRangeUs(selectionStartUs, selectionEndUs, durationUs);
  return range.endUs > range.startUs ? range : null;
}

const WaveformCanvas = memo(function WaveformCanvas({
  waveform,
  durationUs,
  selection,
  onSeek,
  onSelectionChange,
}: {
  waveform: WaveformPayload;
  durationUs: number;
  selection: TimeRangeUs | null;
  onSeek: (positionUs: number) => void;
  onSelectionChange: (startUs: number | null, endUs: number | null) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const size = useCanvasSize(ref);
  const dragRef = useRef<DragState | null>(null);
  const [previewSelection, setPreviewSelection] = useState<TimeRangeUs | null>(null);
  const visibleSelection = previewSelection ?? selection;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;
    const bytes = decodeBase64(waveform.data_base64);
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#191e21";
    context.fillRect(0, 0, width, height);

    if (visibleSelection && durationUs > 0) {
      const startX = (visibleSelection.startUs / durationUs) * width;
      const endX = (visibleSelection.endUs / durationUs) * width;
      context.fillStyle = "rgba(243, 154, 50, 0.20)";
      context.fillRect(startX, 0, Math.max(1, endX - startX), height);
      context.strokeStyle = "#f39a32";
      context.globalAlpha = 0.95;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(startX + 0.5, 0);
      context.lineTo(startX + 0.5, height);
      context.moveTo(endX - 0.5, 0);
      context.lineTo(endX - 0.5, height);
      context.stroke();
      context.globalAlpha = 1;
    }

    context.strokeStyle = "#78a9d0";
    context.globalAlpha = 0.86;
    context.lineWidth = 1;
    context.beginPath();
    for (let x = 0; x < width; x += 1) {
      const bucket = Math.min(samples.length / 2 - 1, Math.floor((x / width) * (samples.length / 2)));
      const minimum = samples[bucket * 2] / 32767;
      const maximum = samples[bucket * 2 + 1] / 32767;
      const center = height / 2;
      context.moveTo(x + 0.5, center - maximum * center * 0.86);
      context.lineTo(x + 0.5, center - minimum * center * 0.86);
    }
    context.stroke();
    context.globalAlpha = 1;

  }, [durationUs, visibleSelection, waveform, size]);

  function finishSelection(event: React.PointerEvent<HTMLCanvasElement>): void {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const focusUs = pointerTime(event, durationUs);
    const range = normalizeRangeUs(drag.anchorUs, focusUs, durationUs, 10_000);
    const moved = Math.abs(event.clientX - drag.startX) >= 4;
    dragRef.current = null;
    setPreviewSelection(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (moved) {
      onSelectionChange(range.startUs, range.endUs);
    } else {
      onSeek(focusUs);
    }
  }

  return (
    <canvas
      ref={ref}
      className="waveform-canvas"
      aria-label="波形；拖动选择区间，单击定位"
      onPointerDown={(event) => {
        if (durationUs <= 0 || event.button !== 0) return;
        const anchorUs = pointerTime(event, durationUs);
        dragRef.current = { pointerId: event.pointerId, startX: event.clientX, anchorUs };
        event.currentTarget.setPointerCapture(event.pointerId);
        setPreviewSelection({ startUs: anchorUs, endUs: anchorUs });
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        setPreviewSelection(
          normalizeRangeUs(drag.anchorUs, pointerTime(event, durationUs), durationUs, 10_000),
        );
      }}
      onPointerUp={finishSelection}
      onPointerCancel={(event) => {
        dragRef.current = null;
        setPreviewSelection(null);
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
    />
  );
});

const SpectrogramCanvas = memo(function SpectrogramCanvas({
  image,
  durationUs,
  selection,
  onSeek,
  onSelectionChange,
}: {
  image: SpectrogramImage;
  durationUs: number;
  selection: TimeRangeUs | null;
  onSeek: (positionUs: number) => void;
  onSelectionChange: (startUs: number | null, endUs: number | null) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const size = useCanvasSize(ref);
  const dragRef = useRef<DragState | null>(null);
  const [previewSelection, setPreviewSelection] = useState<TimeRangeUs | null>(null);
  const visibleSelection = previewSelection ?? selection;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;
    const bytes = decodeBase64(image.data_base64);
    const pixels = context.createImageData(image.width, image.height);
    for (let index = 0; index < bytes.length; index += 1) {
      const [red, green, blue] = intensityToColor(bytes[index]);
      const target = index * 4;
      pixels.data[target] = red;
      pixels.data[target + 1] = green;
      pixels.data[target + 2] = blue;
      pixels.data[target + 3] = 255;
    }
    const buffer = document.createElement("canvas");
    buffer.width = image.width;
    buffer.height = image.height;
    buffer.getContext("2d")?.putImageData(pixels, 0, 0);
    context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    context.drawImage(buffer, 0, 0, canvas.clientWidth, canvas.clientHeight);
    if (visibleSelection && durationUs > 0) {
      const width = canvas.clientWidth;
      const startX = (visibleSelection.startUs / durationUs) * width;
      const endX = (visibleSelection.endUs / durationUs) * width;
      context.fillStyle = "rgba(243, 154, 50, 0.20)";
      context.fillRect(startX, 0, Math.max(1, endX - startX), canvas.clientHeight);
      context.strokeStyle = "#f39a32";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(startX + 0.5, 0);
      context.lineTo(startX + 0.5, canvas.clientHeight);
      context.moveTo(endX - 0.5, 0);
      context.lineTo(endX - 0.5, canvas.clientHeight);
      context.stroke();
    }
  }, [durationUs, image, visibleSelection, size]);

  function finishSelection(event: React.PointerEvent<HTMLCanvasElement>): void {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const focusUs = pointerTime(event, durationUs);
    const range = normalizeRangeUs(drag.anchorUs, focusUs, durationUs, 10_000);
    const moved = Math.abs(event.clientX - drag.startX) >= 4;
    dragRef.current = null;
    setPreviewSelection(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (moved) onSelectionChange(range.startUs, range.endUs);
    else onSeek(focusUs);
  }

  return (
    <canvas
      ref={ref}
      className="spectrogram-canvas"
      aria-label="频谱图；拖动选择区间，单击定位"
      onPointerDown={(event) => {
        if (durationUs <= 0 || event.button !== 0) return;
        const anchorUs = pointerTime(event, durationUs);
        dragRef.current = { pointerId: event.pointerId, startX: event.clientX, anchorUs };
        event.currentTarget.setPointerCapture(event.pointerId);
        setPreviewSelection({ startUs: anchorUs, endUs: anchorUs });
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        setPreviewSelection(
          normalizeRangeUs(drag.anchorUs, pointerTime(event, durationUs), durationUs, 10_000),
        );
      }}
      onPointerUp={finishSelection}
      onPointerCancel={(event) => {
        dragRef.current = null;
        setPreviewSelection(null);
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
    />
  );
});

const WATERFALL_COLUMNS = 256;
const WATERFALL_HEIGHT = 256;

const SpectrumCanvas = memo(function SpectrumCanvas() {
  const spectrum = usePlaybackSpectrum();
  const ref = useRef<HTMLCanvasElement>(null);
  const size = useCanvasSize(ref);
  const bufferRef = useRef<HTMLCanvasElement | null>(null);
  const imageRef = useRef<ImageData | null>(null);
  const lastSpectrum = useRef<typeof spectrum>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;

    let buffer = bufferRef.current;
    let image = imageRef.current;
    if (!buffer || !image) {
      buffer = document.createElement("canvas");
      buffer.width = WATERFALL_COLUMNS;
      buffer.height = WATERFALL_HEIGHT;
      image = buffer.getContext("2d")?.createImageData(WATERFALL_COLUMNS, WATERFALL_HEIGHT) ?? null;
      if (!image) return;
      bufferRef.current = buffer;
      imageRef.current = image;
    }

    if (!spectrum) { image.data.fill(0); lastSpectrum.current = null; buffer.getContext("2d")?.putImageData(image, 0, 0); }
    if (spectrum && spectrum !== lastSpectrum.current) {
      lastSpectrum.current = spectrum;
      const data = image.data;
      data.copyWithin(0, 4);
      const source = spectrum.spectrum;
      const lastColumn = WATERFALL_COLUMNS - 1;
      for (let y = 0; y < WATERFALL_HEIGHT; y += 1) {
        const sourceIndex = Math.min(
          source.length - 1,
          Math.floor(((WATERFALL_HEIGHT - 1 - y) / WATERFALL_HEIGHT) * source.length),
        );
        const [red, green, blue] = intensityToColor(source[sourceIndex] ?? 0);
        const target = (y * WATERFALL_COLUMNS + lastColumn) * 4;
        data[target] = red;
        data[target + 1] = green;
        data[target + 2] = blue;
        data[target + 3] = 255;
      }
      buffer.getContext("2d")?.putImageData(image, 0, 0);
    }

    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    context.fillStyle = "#191e21";
    context.fillRect(0, 0, width, height);
    context.imageSmoothingEnabled = false;
    context.drawImage(buffer, 0, 0, width, height);
  }, [spectrum, size]);
  return <canvas ref={ref} className="waterfall-canvas" />;
});

export default function AnalysisView({
  manifest,
  waveform,
  spectrogram,
  onVisibilityChange,
  selectionStartUs,
  selectionEndUs,
  onSeek,
  onSelectionChange,
  onAnalyzeSelection,
  onTranscribeSelection,
  loopEnabled,
  onToggleLoop,
}: AnalysisViewProps) {
  const sectionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = sectionRef.current;
    if (!element) return;
    let intersecting = false;
    const update = () => onVisibilityChange(intersecting && !document.hidden);
    const observer = new IntersectionObserver(([entry]) => { intersecting = entry.isIntersecting; update(); });
    observer.observe(element);
    document.addEventListener("visibilitychange", update);
    return () => { observer.disconnect(); document.removeEventListener("visibilitychange", update); onVisibilityChange(false); };
  }, [onVisibilityChange]);
  const durationUs = manifest.decode.duration_us;
  const selection = useMemo(
    () => selectionRange(selectionStartUs, selectionEndUs, durationUs),
    [durationUs, selectionEndUs, selectionStartUs],
  );
  const selectionDurationUs = selection ? selection.endUs - selection.startUs : 0;
  return (
    <section ref={sectionRef} className="analysis-view">
      <div className="analysis-header">
        <div>
          <span className="section-number">LIVE ANALYSIS</span>
          <h3>波形、频谱与瀑布图</h3>
        </div>
        <span className="analysis-time"><PlaybackSeconds /></span>
      </div>
      <div className="waveform-stack">
        <WaveformCanvas
          waveform={waveform}
          durationUs={durationUs}
          selection={selection}
          onSeek={onSeek}
          onSelectionChange={onSelectionChange}
        />
        <PlaybackPlayhead durationUs={durationUs} className="analysis-playhead-track" />
      </div>
      <div className="analysis-selection">
        <span>
          {selection
            ? `选区 ${formatRangeTime(selection.startUs)} – ${formatRangeTime(selection.endUs)} · ${(selectionDurationUs / 1_000_000).toFixed(2)}s`
            : "在波形上拖动选择区间；单击波形仍可定位"}
        </span>
        {selection && (
          <div className="analysis-selection-actions">
            <button onClick={() => onSeek(selection.startUs)}>定位起点</button>
            <button className="primary-button" onClick={onAnalyzeSelection}>
              分析选区
            </button>
            <button onClick={onTranscribeSelection}>转录选区</button>
            <button
              className={loopEnabled ? "active-loop" : ""}
              aria-pressed={loopEnabled}
              onClick={onToggleLoop}
            >
              {loopEnabled ? "循环中" : "循环选区"}
            </button>
            <button onClick={() => onSelectionChange(null, null)}>清除</button>
          </div>
        )}
      </div>
      <div className="analysis-grid">
        <div className="analysis-tile">
          <span>全曲频谱图</span>
          {spectrogram ? (
            <div className="spectrogram-stack">
              <SpectrogramCanvas
                image={spectrogram}
                durationUs={durationUs}
                selection={selection}
                onSeek={onSeek}
                onSelectionChange={onSelectionChange}
              />
              <PlaybackPlayhead durationUs={durationUs} className="analysis-playhead-track" />
            </div>
          ) : (
            <div className="analysis-placeholder">生成频谱缓存中…</div>
          )}
        </div>
        <div className="analysis-tile">
          <span>实时分析 / 瀑布图</span>
          <SpectrumCanvas />
        </div>
      </div>
    </section>
  );
}
