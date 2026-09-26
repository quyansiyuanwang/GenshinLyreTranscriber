import { describe, expect, it } from "vitest";

import {
  appendQueueRequests,
  clearCompletedQueue,
  createQueueItem,
  parseQueue,
  pendingQueueIds,
  queueCounts,
  resetInterruptedQueue,
  retryableQueueIds,
  runQueuePlan,
  syncPendingQueue,
} from "./jobQueue";
import type { JobRequest } from "./types";

const request = (input: string, output: string): JobRequest => ({
  input,
  output,
  operation: "transcribe",
  timing: "auto",
  bpm: null,
  transpose: "auto",
  mapping_profile: null,
  mapping_keys: null,
  audio_track: null,
  start_seconds: null,
  end_seconds: null,
  preview_wav: true,
  overwrite: false,
  cleaning_profile: "auto",
  min_confidence: null,
  min_duration_ms: null,
  retrigger_gap_ms: null,
  arrangement: "balanced",
  onset_window_ms: 150,
  max_voices: 2,
  filter: null,
  filter_preset: null,
  worker_path: null,
});

describe("job queue", () => {
  it("deduplicates identical input/output pairs while appending", () => {
    let sequence = 0;
    const first = appendQueueRequests([], [request("a.flac", "a-output")], () => `id-${sequence++}`);
    const second = appendQueueRequests(
      first,
      [request("a.flac", "a-output"), request("b.flac", "b-output")],
      () => `id-${sequence++}`,
    );
    expect(second).toHaveLength(2);
    expect(second[1].request.input).toBe("b.flac");
  });

  it("counts statuses and selects pending or retryable items", () => {
    const items = [
      createQueueItem(request("a", "oa"), "a"),
      { ...createQueueItem(request("b", "ob"), "b"), status: "done" as const },
      { ...createQueueItem(request("c", "oc"), "c"), status: "failed" as const },
      { ...createQueueItem(request("d", "od"), "d"), status: "cancelled" as const },
    ];
    expect(queueCounts(items)).toEqual({
      total: 4,
      pending: 1,
      running: 0,
      done: 1,
      failed: 1,
      cancelled: 1,
    });
    expect(pendingQueueIds(items)).toEqual(["a"]);
    expect(retryableQueueIds(items)).toEqual(["c", "d"]);
  });

  it("clears completed items without removing retry history", () => {
    const items = [
      { ...createQueueItem(request("a", "oa"), "a"), status: "done" as const },
      { ...createQueueItem(request("b", "ob"), "b"), status: "failed" as const },
    ];
    expect(clearCompletedQueue(items).map((item) => item.id)).toEqual(["b"]);
  });

  it("recovers interrupted running items after restart", () => {
    const interrupted = { ...createQueueItem(request("a", "oa"), "a"), status: "running" as const };
    const recovered = resetInterruptedQueue([interrupted]);
    expect(recovered[0].status).toBe("failed");
    expect(recovered[0].error).toContain("上次退出");
  });

  it("rejects corrupted persisted queue data", () => {
    expect(parseQueue("not-json")).toEqual([]);
    expect(parseQueue(JSON.stringify([{ id: "bad" }]))).toEqual([]);
  });

  it("continues after one item fails", async () => {
    const items = [
      createQueueItem(request("good-a", "oa"), "a"),
      createQueueItem(request("bad", "ob"), "b"),
      createQueueItem(request("good-b", "oc"), "c"),
    ];
    const patches: Array<[string, string]> = [];
    const summary = await runQueuePlan({
      items,
      ids: items.map((item) => item.id),
      execute: async (item) => {
        if (item.id === "b") throw new Error("bad input");
      },
      update: (id, patch) => patches.push([id, patch.status ?? ""]),
      stopRequested: () => false,
      consumeSkip: () => false,
      now: () => 1,
    });
    expect(summary.completed).toBe(2);
    expect(summary.failed).toBe(1);
    expect(patches).toContainEqual(["b", "failed"]);
    expect(patches).toContainEqual(["c", "done"]);
  });

  it("updates pending parameters without changing input, output or finished items", () => {
    const items = [
      createQueueItem(request("a", "oa"), "a"),
      { ...createQueueItem(request("b", "ob"), "b"), status: "done" as const },
    ];
    const updated = syncPendingQueue(items, {
      ...request("current", "current-output"),
      cleaning_profile: "strict",
      min_confidence: 0.55,
      transpose: 12,
    });
    expect(updated[0].request).toMatchObject({
      input: "a",
      output: "oa",
      cleaning_profile: "strict",
      min_confidence: 0.55,
      transpose: 12,
    });
    expect(updated[1]).toBe(items[1]);
  });

  it("can skip the current item and continue, or stop the whole queue", async () => {
    const items = [
      createQueueItem(request("a", "oa"), "a"),
      createQueueItem(request("b", "ob"), "b"),
    ];
    let skip = true;
    let stopped = false;
    const skipped = await runQueuePlan({
      items,
      ids: ["a", "b"],
      execute: async () => {
        throw new Error("cancelled");
      },
      update: () => undefined,
      stopRequested: () => stopped,
      consumeSkip: () => {
        const current = skip;
        skip = false;
        return current;
      },
    });
    expect(skipped.skipped).toBe(1);
    expect(skipped.failed).toBe(1);

    let executions = 0;
    const stoppedSummary = await runQueuePlan({
      items,
      ids: ["a", "b"],
      execute: async () => {
        executions += 1;
        stopped = true;
        throw new Error("cancelled");
      },
      update: () => undefined,
      stopRequested: () => stopped,
      consumeSkip: () => false,
    });
    expect(stoppedSummary.cancelled).toBe(1);
    expect(stoppedSummary.stopped).toBe(true);
  });
});
