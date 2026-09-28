import { useSyncExternalStore } from "react";
import type { PlaybackStatus, SpectrumFrame } from "./types";

// High-frequency state is deliberately outside App's render tree.
export function createPlaybackStore() {
  let status: PlaybackStatus = { position_us: 0, paused: true, available: false };
  let spectrum: SpectrumFrame | null = null;
  let generation = 0;
  const listeners = new Set<() => void>();
  const spectralListeners = new Set<() => void>();
  return {
    getStatus: () => status,
    getSpectrum: () => spectrum,
    generation: () => generation,
    invalidate: () => ++generation,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    subscribeSpectrum: (listener: () => void) => {
      spectralListeners.add(listener);
      return () => { spectralListeners.delete(listener); };
    },
    setStatus(next: PlaybackStatus) {
      if (status.position_us === next.position_us && status.paused === next.paused && status.available === next.available) return;
      status = next;
      listeners.forEach((listener) => listener());
    },
    setSpectrum(next: SpectrumFrame | null) {
      spectrum = next;
      spectralListeners.forEach((listener) => listener());
    },
  };
}

// Completion-based scheduling prevents slow IPC from accumulating requests.
export function startSerialPoll(
  task: (isCurrent: () => boolean) => Promise<void>,
  onError: (error: unknown) => void,
  intervalMs = 50,
) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const run = async () => {
    try { await task(() => !stopped); }
    catch (error) { if (!stopped) onError(error); }
    finally { if (!stopped) timer = setTimeout(() => { void run(); }, intervalMs); }
  };
  void run();
  return () => { stopped = true; clearTimeout(timer); };
}

export const playbackStore = createPlaybackStore();
export function usePlaybackStatus() {
  return useSyncExternalStore(playbackStore.subscribe, playbackStore.getStatus, playbackStore.getStatus);
}
export function usePlaybackSpectrum() {
  return useSyncExternalStore(playbackStore.subscribeSpectrum, playbackStore.getSpectrum, playbackStore.getSpectrum);
}
