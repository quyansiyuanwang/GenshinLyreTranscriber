import { useRef } from "react";

interface Props {
  label: string;
  minimum: number;
  maximum: number;
  step: number;
  lower: number;
  upper: number;
  unit?: string;
  onChange: (lower: number, upper: number) => void;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function formatValue(value: number, step: number): string {
  return step < 1 ? value.toFixed(2) : String(Math.round(value));
}

export default function FilterRangeSlider({
  label,
  minimum,
  maximum,
  step,
  lower,
  upper,
  unit = "",
  onChange,
}: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const safeLower = clamp(lower, minimum, maximum);
  const safeUpper = clamp(upper, safeLower, maximum);
  const span = Math.max(step, maximum - minimum);
  const lowerPercent = ((safeLower - minimum) / span) * 100;
  const upperPercent = ((safeUpper - minimum) / span) * 100;

  function update(handle: "lower" | "upper", clientX: number): void {
    const track = trackRef.current;
    if (!track) return;
    const bounds = track.getBoundingClientRect();
    const ratio = clamp((clientX - bounds.left) / Math.max(1, bounds.width), 0, 1);
    const value = minimum + ratio * (maximum - minimum);
    const rounded = Math.round(value / step) * step;
    if (handle === "lower") {
      onChange(clamp(rounded, minimum, safeUpper), safeUpper);
    } else {
      onChange(safeLower, clamp(rounded, safeLower, maximum));
    }
  }

  function handleKey(handle: "lower" | "upper", delta: number): void {
    if (handle === "lower") {
      onChange(clamp(safeLower + delta, minimum, safeUpper), safeUpper);
    } else {
      onChange(safeLower, clamp(safeUpper + delta, safeLower, maximum));
    }
  }

  return (
    <div className="threshold-slider">
      <div className="threshold-slider-heading">
        <span>{label}</span>
        <strong>
          {formatValue(safeLower, step)} – {formatValue(safeUpper, step)} {unit}
        </strong>
      </div>
      <div className="threshold-slider-track" ref={trackRef}>
        <div
          className="threshold-slider-fill"
          style={{ left: `${lowerPercent}%`, width: `${Math.max(0, upperPercent - lowerPercent)}%` }}
        />
        <button
          type="button"
          className="threshold-slider-handle"
          style={{ left: `${lowerPercent}%` }}
          aria-label={`${label} 最小值`}
          title={`${label} 最小值：${formatValue(safeLower, step)} ${unit}`}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            update("lower", event.clientX);
          }}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              update("lower", event.clientX);
            }
          }}
          onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
              event.preventDefault();
              handleKey("lower", -step);
            } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
              event.preventDefault();
              handleKey("lower", step);
            }
          }}
        />
        <button
          type="button"
          className="threshold-slider-handle"
          style={{ left: `${upperPercent}%` }}
          aria-label={`${label} 最大值`}
          title={`${label} 最大值：${formatValue(safeUpper, step)} ${unit}`}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            update("upper", event.clientX);
          }}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              update("upper", event.clientX);
            }
          }}
          onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
              event.preventDefault();
              handleKey("upper", -step);
            } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
              event.preventDefault();
              handleKey("upper", step);
            }
          }}
        />
      </div>
    </div>
  );
}
