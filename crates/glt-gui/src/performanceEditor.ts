import type { PerformanceDocument, PerformanceNote } from "./types";

export const KEYBOARD_ORDER = "ZXCVBNMASDFGHJQWERTYU".split("");
export const KEYBOARD_PITCHES = [
  48, 50, 52, 53, 55, 57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77, 79, 81, 83,
];
export const HISTORY_LIMIT = 100;

export interface HistoryState {
  past: PerformanceNote[][];
  future: PerformanceNote[][];
}

export function emptyHistory(): HistoryState {
  return { past: [], future: [] };
}

export function commitHistory(
  history: HistoryState,
  current: PerformanceNote[],
): HistoryState {
  return {
    past: [...history.past.slice(-(HISTORY_LIMIT - 1)), current],
    future: [],
  };
}

export function undoHistory(
  history: HistoryState,
  current: PerformanceNote[],
): { history: HistoryState; notes: PerformanceNote[] } | null {
  const previous = history.past.at(-1);
  if (!previous) return null;
  return {
    history: {
      past: history.past.slice(0, -1),
      future: [current, ...history.future].slice(0, HISTORY_LIMIT),
    },
    notes: previous,
  };
}

export function redoHistory(
  history: HistoryState,
  current: PerformanceNote[],
): { history: HistoryState; notes: PerformanceNote[] } | null {
  const next = history.future[0];
  if (!next) return null;
  return {
    history: {
      past: [...history.past.slice(-(HISTORY_LIMIT - 1)), current],
      future: history.future.slice(1),
    },
    notes: next,
  };
}

export function pitchForKey(key: string): number {
  const index = KEYBOARD_ORDER.indexOf(key);
  return index >= 0 ? KEYBOARD_PITCHES[index] : 60;
}

export function keyForPitch(pitch: number): string {
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const [index, candidate] of KEYBOARD_PITCHES.entries()) {
    const distance = Math.abs(candidate - pitch);
    if (distance < bestDistance) {
      bestIndex = index;
      bestDistance = distance;
    }
  }
  return KEYBOARD_ORDER[bestIndex];
}

export function pitchName(pitch: number): string {
  const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  return `${names[((pitch % 12) + 12) % 12]}${Math.floor(pitch / 12) - 1}`;
}

export function withNotes(
  document: PerformanceDocument,
  notes: PerformanceNote[],
): PerformanceDocument {
  return {
    ...document,
    notes: [...notes].sort(
      (left, right) =>
        left.start_us - right.start_us || left.pitch - right.pitch || left.id.localeCompare(right.id),
    ),
  };
}

function nearestGridTime(valueUs: number, snapTimes: readonly number[]): number {
  if (snapTimes.length === 0) return valueUs;
  let nearest = snapTimes[0];
  let distance = Math.abs(nearest - valueUs);
  for (const candidate of snapTimes) {
    const candidateDistance = Math.abs(candidate - valueUs);
    if (candidateDistance < distance) {
      nearest = candidate;
      distance = candidateDistance;
    }
  }
  return nearest;
}

export function moveNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
  deltaUs: number,
  deltaPitch: number,
  durationUs: number,
  snapTimes: readonly number[] = [],
): PerformanceNote[] {
  const anchor = notes.find((note) => selected.has(note.id));
  const snappedDelta = anchor
    ? nearestGridTime(anchor.start_us + deltaUs, snapTimes) - anchor.start_us
    : deltaUs;
  return notes.map((note) => {
    if (!selected.has(note.id)) return note;
    const length = note.end_us - note.start_us;
    const pitch = Math.max(0, Math.min(127, note.pitch + deltaPitch));
    const key = keyForPitch(pitch);
    const mappedPitch = pitchForKey(key);
    const start = Math.max(0, Math.min(durationUs - length, note.start_us + snappedDelta));
    return {
      ...note,
      id: note.id,
      key,
      pitch: mappedPitch,
      original_pitch: note.original_pitch + deltaPitch,
      pitch_center: note.pitch_center + deltaPitch,
      start_us: start,
      end_us: start + length,
    };
  });
}

export function resizeNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
  deltaUs: number,
  durationUs: number,
  snapTimes: readonly number[] = [],
): PerformanceNote[] {
  return notes.map((note) => {
    if (!selected.has(note.id)) return note;
    const desired = Math.min(durationUs, note.end_us + deltaUs);
    const snapped = nearestGridTime(desired, snapTimes);
    const end = Math.max(note.start_us + 10_000, Math.min(durationUs, snapped));
    return { ...note, end_us: end };
  });
}

export function setVelocities(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
  velocity: number,
): PerformanceNote[] {
  const bounded = Math.max(1, Math.min(127, Math.round(velocity)));
  return notes.map((note) => (selected.has(note.id) ? { ...note, velocity: bounded } : note));
}

export function duplicateNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
  offsetUs: number,
  durationUs: number,
): PerformanceNote[] {
  const chosen = notes.filter((note) => selected.has(note.id));
  if (chosen.length === 0) return notes;
  const duplicates = chosen.map((note, index) => {
    const length = note.end_us - note.start_us;
    const start = Math.max(0, Math.min(durationUs - length, note.start_us + offsetUs));
    return {
      ...note,
      id: `${note.id}-copy-${index}`,
      start_us: start,
      end_us: start + length,
      source_stem: "edit",
      candidate_id: null,
    };
  });
  return [...notes, ...duplicates];
}

export function deleteNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
): PerformanceNote[] {
  return notes.filter((note) => !selected.has(note.id));
}

export function addNote(
  notes: PerformanceNote[],
  startUs: number,
  key: string,
  durationUs: number,
): { notes: PerformanceNote[]; id: string } {
  const pitch = pitchForKey(key);
  const existing = notes.filter((note) => note.start_us === startUs && note.key === key).length;
  const id = `edit-${Date.now().toString(36)}-${notes.length}-${existing}`;
  const start = Math.max(0, Math.min(durationUs - 10_000, Math.round(startUs)));
  return {
    id,
    notes: [
      ...notes,
      {
        id,
        start_us: start,
        end_us: Math.min(durationUs, start + Math.max(10_000, durationUs / 20)),
        key,
        pitch,
        velocity: 88,
        confidence: null,
        source_stem: "edit",
        candidate_id: null,
        original_pitch: pitch,
        pitch_center: pitch,
        pitch_bend_class: "stable",
        pitch_bends: [],
      },
    ],
  };
}

export function splitNote(
  notes: PerformanceNote[],
  noteId: string,
  atUs: number,
): PerformanceNote[] {
  const note = notes.find((value) => value.id === noteId);
  if (!note || atUs <= note.start_us + 10_000 || atUs >= note.end_us - 10_000) return notes;
  const left = { ...note, id: `${note.id}-a`, end_us: atUs };
  const right = { ...note, id: `${note.id}-b`, start_us: atUs };
  return notes.flatMap((value) => (value.id === noteId ? [left, right] : [value]));
}

export function mergeNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
): PerformanceNote[] {
  const chosen = notes.filter((note) => selected.has(note.id));
  if (chosen.length < 2) return notes;
  const start = Math.min(...chosen.map((note) => note.start_us));
  const end = Math.max(...chosen.map((note) => note.end_us));
  const pitch = chosen[0].pitch;
  const merged: PerformanceNote = {
    ...chosen[0],
    id: `${chosen[0].id}-merged`,
    start_us: start,
    end_us: end,
    pitch,
    key: keyForPitch(pitch),
    pitch_center: pitch,
    pitch_bends: [],
    pitch_bend_class: "stable",
    candidate_id: null,
    source_stem: "edit",
  };
  return [...notes.filter((note) => !selected.has(note.id)), merged];
}

export function quantizeNotes(
  notes: PerformanceNote[],
  selected: ReadonlySet<string>,
  gridUs: number,
  durationUs: number,
): PerformanceNote[] {
  if (gridUs <= 0) return notes;
  return notes.map((note) => {
    if (!selected.has(note.id)) return note;
    const length = note.end_us - note.start_us;
    const start = Math.max(
      0,
      Math.min(durationUs - length, Math.round(note.start_us / gridUs) * gridUs),
    );
    return { ...note, start_us: start, end_us: start + length };
  });
}
