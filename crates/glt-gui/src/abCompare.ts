import type { PlaybackStatus } from "./types";

export interface AbSourceOption {
  id: string;
  label: string;
  path: string | null;
  primary: "original" | "mapped" | "extra";
  alignment?: AbAlignment | null;
}

export function abSwitchPosition(status: PlaybackStatus, fallbackUs: number): number {
  return status.available ? status.position_us : Math.max(0, fallbackUs);
}

export function findAbSource(
  options: AbSourceOption[],
  sourceId: string,
): AbSourceOption | undefined {
  return options.find((option) => option.id === sourceId);
}

export function missingAbSourceMessage(option: AbSourceOption | undefined): string {
  return `没有可播放的${option?.label ?? "音频"}来源`;
}

export interface AbAlignment { sourceHash: string; offsetUs: number; durationUs: number }
export function validAlignment(value: AbAlignment | null | undefined): value is AbAlignment {
  return !!value && /^[0-9a-f]{64}$/.test(value.sourceHash) && Number.isSafeInteger(value.offsetUs) && value.offsetUs >= 0 && Number.isSafeInteger(value.durationUs) && value.durationUs > 0 && Number.isSafeInteger(value.offsetUs + value.durationUs);
}
export function positionOnTimeline(positionUs: number, source: AbAlignment | null, targetHash: string | null, targetOffsetUs: number): number | null {
  return validAlignment(source) && targetHash === source.sourceHash ? positionUs + source.offsetUs - targetOffsetUs : null;
}
export function planAbSwitch(from: AbSourceOption | null, to: AbSourceOption, positionUs: number, available: boolean): { positionUs: number; message: string; aligned: boolean } {
  if (!available || !from) return { positionUs: 0, aligned: false, message: validAlignment(to.alignment) ? "从音源起点播放" : "缺少可靠时间元数据，独立试听" };
  if (from.path === to.path) return { positionUs: Math.max(0, Math.min(positionUs, validAlignment(to.alignment) ? to.alignment.durationUs - 1 : positionUs)), aligned: validAlignment(to.alignment), message: "继续当前音源" };
  if (!validAlignment(from.alignment) || !validAlignment(to.alignment) || from.alignment.sourceHash !== to.alignment.sourceHash) return { positionUs: 0, aligned: false, message: "无法确认同一素材与时间偏移，已降级为独立试听（从起点）" };
  const start = Math.max(from.alignment.offsetUs, to.alignment.offsetUs);
  const end = Math.min(from.alignment.offsetUs + from.alignment.durationUs, to.alignment.offsetUs + to.alignment.durationUs);
  if (end <= start) return { positionUs: 0, aligned: false, message: "两个音源没有公共时间范围，已降级为独立试听" };
  const point = from.alignment.offsetUs + positionUs;
  const clamped = Math.max(start, Math.min(end - 1, point));
  return { positionUs: clamped - to.alignment.offsetUs, aligned: true, message: point === clamped ? `已对齐原素材 ${(clamped / 1_000_000).toFixed(2)}s` : `超出公共范围，已定位到有效边界 ${(clamped / 1_000_000).toFixed(2)}s` };
}
