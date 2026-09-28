import { afterEach, describe, expect, it, vi } from "vitest";
import { createPlaybackStore, startSerialPoll, createPollLane, MAX_EXTRAPOLATION_MS } from "./playbackRuntime";

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

describe("playback session safety", () => {
  it("interpolates only within the bounded sample window and resynchronizes after seek", () => {
    let time = 0;
    const store = createPlaybackStore(() => time);
    store.setStatus({ position_us: 1_000_000, available: true, paused: false });
    time = 50;
    expect(store.getPosition()).toBe(1_050_000);
    time = 10_000;
    expect(store.getPosition()).toBe(1_000_000 + MAX_EXTRAPOLATION_MS * 1000);
    store.invalidate();
    expect(store.getPosition()).toBe(1_000_000);
    store.setStatus({ position_us: 20_000, available: true, paused: true });
    time += 500;
    expect(store.getPosition()).toBe(20_000);
    store.setStatus({ position_us: 40_000, available: true, paused: false });
    time += 10;
    expect(store.getPosition()).toBe(50_000);
    store.freeze();
    expect(store.getPosition()).toBe(40_000);
  });

  it("resamples unchanged backend positions without broadcasting to static consumers", () => {
    let time = 0;
    const store = createPlaybackStore(() => time);
    const listener = vi.fn(); store.subscribe(listener);
    const status = { position_us: 1000, available: true, paused: false };
    store.setStatus(status); time = 50;
    store.setStatus(status);
    expect(store.getPosition()).toBe(1000);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("shares a single-flight lane across replacement effects and discards queued obsolete sessions", async () => {
    vi.useFakeTimers();
    const lane = createPollLane();
    let finish!: () => void;
    const publish = vi.fn();
    const old = vi.fn(async (current: () => boolean) => {
      await new Promise<void>((resolve) => { finish = resolve; });
      if (current()) publish("old");
    });
    const stopOld = startSerialPoll(old, vi.fn(), 50, lane);
    await vi.advanceTimersByTimeAsync(0);
    stopOld();
    const obsolete = vi.fn(async () => { publish("obsolete"); });
    const stopObsolete = startSerialPoll(obsolete, vi.fn(), 50, lane);
    stopObsolete();
    const latest = vi.fn(async () => { publish("latest"); });
    const stopLatest = startSerialPoll(latest, vi.fn(), 50, lane);
    await vi.advanceTimersByTimeAsync(500);
    expect(latest).not.toHaveBeenCalled();
    finish();
    await vi.advanceTimersByTimeAsync(50);
    expect(old).toHaveBeenCalledTimes(1);
    expect(obsolete).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);
    expect(publish.mock.calls).toEqual([["latest"]]);
    stopLatest();
  });

  it("releases the lane on rejection so a new session can recover", async () => {
    vi.useFakeTimers();
    const lane = createPollLane();
    let reject!: (reason: Error) => void;
    const staleError = vi.fn();
    const stop = startSerialPoll(async () => new Promise<void>((_, fail) => { reject = fail; }), staleError, 50, lane);
    await vi.advanceTimersByTimeAsync(0);
    stop();
    const next = vi.fn(async () => undefined);
    const stopNext = startSerialPoll(next, vi.fn(), 50, lane);
    reject(new Error("late failure"));
    await vi.advanceTimersByTimeAsync(50);
    expect(staleError).not.toHaveBeenCalled();
    expect(next).toHaveBeenCalledTimes(1);
    stopNext();
  });
});
