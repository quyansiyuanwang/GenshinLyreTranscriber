import { useEffect, useState, type RefObject } from "react";

// ResizeObserver covers dock/viewport changes; a resolution query also catches
// monitor/DPI changes where the CSS size itself remains unchanged.
export function useCanvasSize(ref: RefObject<HTMLCanvasElement | null>) {
  const [size, setSize] = useState("");
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let query: MediaQueryList;
    const measure = () => setSize(`${canvas.clientWidth}:${canvas.clientHeight}:${window.devicePixelRatio}`);
    const watchDpi = () => {
      query?.removeEventListener("change", watchDpi);
      query = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
      query.addEventListener("change", watchDpi);
      measure();
    };
    const observer = new ResizeObserver(measure);
    observer.observe(canvas);
    watchDpi();
    return () => { observer.disconnect(); query.removeEventListener("change", watchDpi); };
  }, [ref]);
  return size;
}
