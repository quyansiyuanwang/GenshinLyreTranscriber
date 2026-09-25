import { describe, expect, it } from "vitest";

import {
  HISTORY_LIMIT,
  commitHistory,
  deleteNotes,
  emptyHistory,
  mergeNotes,
  moveNotes,
  redoHistory,
  resizeNotes,
  splitNote,
  undoHistory,
  withNotes,
} from "./performanceEditor";
import type { PerformanceDocument, PerformanceNote } from "./types";

function note(id: string, start: number, end: number, key: string, pitch: number): PerformanceNote {
  return {
    id,
    start_us: start,
    end_us: end,
    key,
    pitch,
    velocity: 90,
    confidence: 0.8,
    source_stem: "mix",
    candidate_id: null,
    original_pitch: pitch,
    pitch_center: pitch,
    pitch_bend_class: "stable",
    pitch_bends: [],
  };
}

function document(notes: PerformanceNote[]): PerformanceDocument {
  return {
    format_version: 1,
    time_unit: "us",
    duration_us: 2_000_000,
    revision: { id: "performance-000", parent_id: null, source: "transcribe" },
    source: { type: "audio", offset_us: 0 },
    mapping: { profile: "lyre-21-default", transpose_semitones: 0 },
    tempo_map: [],
    beat_grid: [],
    notes,
  };
}

describe("performance editor history", () => {
  it("keeps at most one hundred undo steps and redoes them", () => {
    let history = emptyHistory();
    let notes = [note("a", 0, 100_000, "A", 60)];
    for (let index = 0; index < HISTORY_LIMIT + 20; index += 1) {
      history = commitHistory(history, notes);
      notes = [{ ...notes[0], velocity: (index % 127) + 1 }];
    }
    expect(history.past).toHaveLength(HISTORY_LIMIT);
    const undone = undoHistory(history, notes);
    expect(undone).not.toBeNull();
    const redone = redoHistory(undone!.history, undone!.notes);
    expect(redone).not.toBeNull();
    expect(redone!.notes).toEqual(notes);
  });
});

describe("performance edits", () => {
  const original = [note("a", 100_000, 500_000, "A", 60), note("b", 600_000, 900_000, "D", 64)];

  it("moves time and snaps pitch to a valid lyre key", () => {
    const moved = moveNotes(original, new Set(["a"]), 100_000, 3, 2_000_000);
    expect(moved[0].start_us).toBe(200_000);
    expect(moved[0].pitch).toBe(62);
    expect(moved[0].key).toBe("S");
  });

  it("snaps move and resize operations to real beat times", () => {
    const moved = moveNotes(original, new Set(["a"]), 40_000, 0, 2_000_000, [0, 250_000, 500_000]);
    expect(moved[0].start_us).toBe(250_000);
    const resized = resizeNotes(
      [{ ...original[0], end_us: 480_000 }],
      new Set(["a"]),
      40_000,
      2_000_000,
      [0, 250_000, 500_000],
    );
    expect(resized[0].end_us).toBe(500_000);
  });

  it("resizes, splits, merges and deletes without touching other notes", () => {
    const resized = resizeNotes(original, new Set(["a"]), 100_000, 2_000_000);
    expect(resized[0].end_us).toBe(600_000);
    const split = splitNote(resized, "a", 350_000);
    expect(split.filter((value) => value.id.startsWith("a"))).toHaveLength(2);
    const merged = mergeNotes(split, new Set(["a-a", "a-b"]));
    expect(merged.some((value) => value.id === "a-a-merged")).toBe(true);
    const remaining = deleteNotes(original, new Set(["b"]));
    expect(remaining.map((value) => value.id)).toEqual(["a"]);
    expect(withNotes(document(original), remaining).notes).toHaveLength(1);
  });
});
