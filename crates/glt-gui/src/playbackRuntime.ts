import { useSyncExternalStore } from "react";
import type { PlaybackStatus, SpectrumFrame } from "./types";

// High-frequency state is deliberately outside App's render tree.
export const MAX_EXTRAPOLATION_MS = 150;

export function createPlaybackStore(now: () => number = () => performance.now()) {
  let sampledAt = now();
  let synchronized = false;
  let status: PlaybackStatus = { position_us: 0, paused: true, available: false };
  let spectrum: SpectrumFrame | null = null;
  let generation = 0;
  const listeners = new Set<() => void>();
  const spectralListeners = new Set<() => void>();
  return {
    getStatus: () => status,
    getSpectrum: () => spectrum,
    generation: () => generation,
    invalidate() {
      synchronized = false;
      return ++generation;
    },
    freeze() { synchronized = false; },
    getPosition(nowMs = now()) {
      if (!synchronized || status.paused || !status.available) return status.position_us;
      return status.position_us + Math.max(0, Math.min(MAX_EXTRAPOLATION_MS, nowMs - sampledAt)) * 1000;
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    subscribeSpectrum: (listener: () => void) => {
      spectralListeners.add(listener);
      return () => { spectralListeners.delete(listener); };
    },
    setStatus(next: PlaybackStatus) {
      sampledAt = now();
      synchronized = true;
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

// A lane survives effect cleanup/restart: a replacement retries after the old IPC
// finishes rather than creating a second request or accumulating waiters.
export function createPollLane() {
  let inFlight: Promise<void> | null = null;
  return async (task: () => Promise<void>, isCurrent: () => boolean) => {
    if (inFlight || !isCurrent()) return;
    const pending = Promise.resolve().then(() => isCurrent() ? task() : undefined);
    inFlight = pending;
    try { await pending; }
    finally { if (inFlight === pending) inFlight = null; }
  };
}

// Completion-based scheduling prevents slow IPC from accumulating requests.
export function startSerialPoll(
  task: (isCurrent: () => boolean) => Promise<void>,
  onError: (error: unknown) => void,
  intervalMs = 50,
  lane = createPollLane(),
) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const run = async () => {
    try { await lane(() => task(() => !stopped), () => !stopped); }
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
