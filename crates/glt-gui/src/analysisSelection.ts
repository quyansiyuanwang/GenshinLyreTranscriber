export interface TimeRangeUs {
  startUs: number;
  endUs: number;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function normalizeRangeUs(
  anchorUs: number,
  focusUs: number,
  durationUs: number,
  minimumDurationUs = 1000,
): TimeRangeUs {
  const safeDuration = Math.max(0, durationUs);
  const minimum = Math.min(Math.max(0, minimumDurationUs), safeDuration);
  const anchor = clamp(anchorUs, 0, safeDuration);
  const focus = clamp(focusUs, 0, safeDuration);
  let startUs = Math.min(anchor, focus);
  let endUs = Math.max(anchor, focus);
  if (endUs - startUs < minimum) {
    if (startUs + minimum <= safeDuration) {
      endUs = startUs + minimum;
    } else {
      endUs = safeDuration;
      startUs = Math.max(0, endUs - minimum);
    }
  }
  return { startUs, endUs };
}

export function formatRangeTime(valueUs: number): string {
  const totalSeconds = Math.max(0, valueUs) / 1_000_000;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`;
}
