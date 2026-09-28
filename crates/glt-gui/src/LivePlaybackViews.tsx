import { useEffect, useRef } from "react";
import { playbackStore, usePlaybackStatus } from "./playbackRuntime";

// Only the clock subscribes to status. Editors and static canvases never do.
export function PlaybackClock() {
  const { position_us } = usePlaybackStatus();
  const seconds = Math.max(0, position_us / 1_000_000);
  return <>{Math.floor(seconds / 60).toString().padStart(2, "0")}:{Math.floor(seconds % 60).toString().padStart(2, "0")}.{Math.floor((seconds % 1) * 1000).toString().padStart(3, "0")}</>;
}

export function PlaybackSeconds() {
  const { position_us } = usePlaybackStatus();
  return <>{(position_us / 1_000_000).toFixed(2)}s</>;
}

export function PlaybackPlayhead({
  startUs = 0, durationUs, className, clip = false,
}: { startUs?: number; durationUs: number; className: string; clip?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    let frame = 0;
    let visible = true;
    let disposed = false;
    const draw = () => {
      frame = 0;
      if (disposed || document.hidden || !visible) return;
      const ratio = durationUs > 0 ? (playbackStore.getPosition() - startUs) / durationUs : -1;
      element.style.visibility = durationUs <= 0 || (clip && (ratio < 0 || ratio > 1)) ? "hidden" : "visible";
      element.style.transform = `translateX(${Math.max(0, Math.min(1, ratio)) * 100}%)`;
      const status = playbackStore.getStatus();
      if (status.available && !status.paused) frame = requestAnimationFrame(draw);
    };
    const sync = () => { cancelAnimationFrame(frame); draw(); };
    // Observe the stationary viewport, not the moving playhead.
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; sync(); });
    observer.observe(element.parentElement ?? element);
    document.addEventListener("visibilitychange", sync);
    const unsubscribe = playbackStore.subscribe(sync);
    draw();
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      unsubscribe();
      observer.disconnect();
      document.removeEventListener("visibilitychange", sync);
    };
  }, [startUs, durationUs, clip]);
  return <div ref={ref} className={className} aria-hidden="true"><i /></div>;
}
