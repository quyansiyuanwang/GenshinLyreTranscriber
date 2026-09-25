export function recentTapTimes(times: number[], now: number, windowMs = 2200): number[] {
  return [...times, now].filter((time) => now - time <= windowMs).slice(-8);
}

export function estimateBpm(times: number[]): number | null {
  if (times.length < 2) return null;
  const intervals = times.slice(1).map((time, index) => time - times[index]);
  const average = intervals.reduce((total, value) => total + value, 0) / intervals.length;
  if (!Number.isFinite(average) || average <= 0) return null;
  const bpm = Math.round((60_000 / average) * 10) / 10;
  return bpm >= 20 && bpm <= 400 ? bpm : null;
}
