import { afterEach, describe, expect, it, vi } from "vitest";
import { createPlaybackStore, startSerialPoll } from "./playbackRuntime";

afterEach(() => vi.useRealTimers());
describe("playback runtime", () => {
  it("keeps playback notifications separate from spectrum and skips unchanged status", () => {
    const store = createPlaybackStore();
    const status = vi.fn(); const spectrum = vi.fn();
    const unsubscribe = store.subscribe(status);
    store.subscribeSpectrum(spectrum);
    store.setStatus({ position_us: 100, paused: false, available: true });
    store.setStatus({ position_us: 100, paused: false, available: true });
    expect(status).toHaveBeenCalledTimes(1);
    expect(spectrum).not.toHaveBeenCalled();
    unsubscribe();
    store.setStatus({ position_us: 200, paused: false, available: true });
    expect(status).toHaveBeenCalledTimes(1);
    const old = store.generation(); store.invalidate();
    expect(store.generation()).not.toBe(old);
  });
  it("never overlaps slow requests and rejects completion after disposal", async () => {
    vi.useFakeTimers();
    let finish!: () => void;
    const publish = vi.fn();
    const task = vi.fn(async (isCurrent: () => boolean) => {
      await new Promise<void>((resolve) => { finish = resolve; });
      if (isCurrent()) publish();
    });
    const stop = startSerialPoll(task, vi.fn());
    await vi.advanceTimersByTimeAsync(500);
    expect(task).toHaveBeenCalledTimes(1);
    stop(); finish();
    await vi.advanceTimersByTimeAsync(500);
    expect(publish).not.toHaveBeenCalled();
    expect(task).toHaveBeenCalledTimes(1);
  });
  it("reports errors and continues polling without unhandled rejection", async () => {
    vi.useFakeTimers();
    const onError = vi.fn(); const task = vi.fn().mockRejectedValue(new Error("offline"));
    const stop = startSerialPoll(task, onError);
    await vi.advanceTimersByTimeAsync(100);
    stop();
    expect(onError).toHaveBeenCalledTimes(3);
  });
});
