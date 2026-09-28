import type { ComponentProps } from "react";
import AnalysisView from "./AnalysisView";
import PianoRollEditor from "./PianoRollEditor";
import { usePlaybackStatus, usePlaybackSpectrum } from "./playbackRuntime";

export function PlaybackClock() {
  const { position_us } = usePlaybackStatus();
  const seconds = Math.max(0, position_us / 1_000_000);
  return <>{Math.floor(seconds / 60).toString().padStart(2, "0")}:{Math.floor(seconds % 60).toString().padStart(2, "0")}.{Math.floor((seconds % 1) * 1000).toString().padStart(3, "0")}</>;
}
export function LiveAnalysisView(props: Omit<ComponentProps<typeof AnalysisView>, "positionUs" | "spectrum">) {
  const status = usePlaybackStatus();
  const spectrum = usePlaybackSpectrum();
  return <AnalysisView {...props} positionUs={status.position_us} spectrum={spectrum} />;
}
export function LivePianoRollEditor(props: Omit<ComponentProps<typeof PianoRollEditor>, "positionUs">) {
  const status = usePlaybackStatus();
  return <PianoRollEditor {...props} positionUs={status.position_us} />;
}
