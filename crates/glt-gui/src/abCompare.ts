import type { PlaybackStatus } from "./types";

export interface AbSourceOption {
  id: string;
  label: string;
  path: string | null;
  primary: "original" | "mapped" | "extra";
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
