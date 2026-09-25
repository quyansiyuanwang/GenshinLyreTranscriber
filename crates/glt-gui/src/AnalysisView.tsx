import { useEffect, useRef } from "react";

import type {
  AnalysisManifest,
  SpectrogramImage,
  SpectrumFrame,
  WaveformPayload,
} from "./types";

interface AnalysisViewProps {
  manifest: AnalysisManifest;
  waveform: WaveformPayload;
  spectrogram: SpectrogramImage | null;
  spectrum: SpectrumFrame | null;
  positionUs: number;
  onSeek: (positionUs: number) => void;
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

function WaveformCanvas({
  waveform,
  durationUs,
  positionUs,
  onSeek,
}: {
  waveform: WaveformPayload;
  durationUs: number;
  positionUs: number;
  onSeek: (positionUs: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
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
    context.strokeStyle = "#78a9d0";
    context.globalAlpha = 0.82;
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
    const playhead = durationUs > 0 ? Math.min(1, positionUs / durationUs) : 0;
    context.strokeStyle = "#f39a32";
    context.lineWidth = 2;
    context.beginPath();
    context.moveTo(playhead * width, 0);
    context.lineTo(playhead * width, height);
    context.stroke();
  }, [durationUs, positionUs, waveform]);

  return (
    <canvas
      ref={ref}
      className="waveform-canvas"
      onClick={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
        onSeek(Math.round(ratio * durationUs));
      }}
    />
  );
}

function SpectrogramCanvas({
  image,
  durationUs,
  positionUs,
}: {
  image: SpectrogramImage;
  durationUs: number;
  positionUs: number;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
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
    const playhead = durationUs > 0 ? Math.min(1, positionUs / durationUs) : 0;
    context.strokeStyle = "#f39a32";
    context.globalAlpha = 0.9;
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(playhead * canvas.clientWidth, 0);
    context.lineTo(playhead * canvas.clientWidth, canvas.clientHeight);
    context.stroke();
    context.globalAlpha = 1;
  }, [durationUs, image, positionUs]);

  return <canvas ref={ref} className="spectrogram-canvas" />;
}

function SpectrumCanvas({ spectrum }: { spectrum: SpectrumFrame | null }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const waterfall = useRef<number[][]>([]);
  useEffect(() => {
    if (spectrum) {
      waterfall.current = [...waterfall.current.slice(-255), spectrum.spectrum];
    }
    const canvas = ref.current;
    if (!canvas) return;
    const context = setupCanvas(canvas);
    if (!context) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    context.fillStyle = "#191e21";
    context.fillRect(0, 0, width, height);
    const history = waterfall.current;
    if (history.length === 0) return;
    const columnWidth = Math.max(1, width / 256);
    history.forEach((column, columnIndex) => {
      const x = width - (history.length - columnIndex) * columnWidth;
      column.forEach((value, bin) => {
        const y = height - (bin / column.length) * height;
        const [red, green, blue] = intensityToColor(value);
        context.fillStyle = `rgb(${red} ${green} ${blue})`;
        context.fillRect(x, y, columnWidth + 0.5, Math.max(1, height / column.length));
      });
    });
  }, [spectrum]);
  return <canvas ref={ref} className="waterfall-canvas" />;
}

export default function AnalysisView({
  manifest,
  waveform,
  spectrogram,
  spectrum,
  positionUs,
  onSeek,
}: AnalysisViewProps) {
  return (
    <section className="analysis-view">
      <div className="analysis-header">
        <div>
          <span className="section-number">LIVE ANALYSIS</span>
          <h3>波形、频谱与瀑布图</h3>
        </div>
        <span className="analysis-time">{(positionUs / 1_000_000).toFixed(2)}s</span>
      </div>
      <WaveformCanvas
        waveform={waveform}
        durationUs={manifest.decode.duration_us}
        positionUs={positionUs}
        onSeek={onSeek}
      />
      <div className="analysis-grid">
        <div className="analysis-tile">
          <span>全曲频谱图</span>
          {spectrogram ? (
            <SpectrogramCanvas
              image={spectrogram}
              durationUs={manifest.decode.duration_us}
              positionUs={positionUs}
            />
          ) : (
            <div className="analysis-placeholder">生成频谱缓存中…</div>
          )}
        </div>
        <div className="analysis-tile">
          <span>实时分析 / 瀑布图</span>
          <SpectrumCanvas spectrum={spectrum} />
        </div>
      </div>
    </section>
  );
}
