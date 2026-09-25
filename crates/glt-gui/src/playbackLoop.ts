export interface LoopRangeUs {
  startUs: number;
  endUs: number;
}

export function shouldLoopSeek(
  positionUs: number,
  range: LoopRangeUs | null,
  enabled: boolean,
  paused: boolean,
  available: boolean,
): boolean {
  return (
    enabled &&
    available &&
    !paused &&
    range !== null &&
    range.endUs > range.startUs &&
    positionUs >= range.endUs
  );
}
