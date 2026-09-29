export function percentile(values, fraction) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.floor((ordered.length - 1) * fraction))];
}
const heap = (entry) => entry?.metrics.find((m) => m.name === "JSHeapUsedSize")?.value ?? Infinity;
export function summarizeGate(report) {
  const frameP95 = percentile(report.frames, .95), median = percentile(report.frames, .5);
  const hz = median > 0 ? 1000 / median : 0;
  const inputP95 = percentile(report.interactions.map((e) => e.ms), .95);
  const errorMax = report.positions.length ? report.positions.reduce((max,p)=>Math.max(max,Math.abs(p.error_ms)+p.uncertainty_ms),0) : Infinity;
  const before = report.resources.find((r) => r.phase === "before-churn"), after = report.resources.find((r) => r.phase === "after-churn");
  const checks = {
    visibleNativeWindow: report.viewport?.nativeVisible === true && report.viewport.hidden === false,
    fullTenMinuteFile: report.seconds >= 600 && report.measuredSteadySeconds >= 600 && report.continuityChecks >= report.seconds * 10 && report.discontinuities.length === 0 && report.sourceDurationUs >= 600_000_000 && report.requestedStarts.includes(0) && report.firstPositionUs <= 250_000 && report.lastPositionUs >= report.sourceDurationUs - 100_000,
    nonzeroSeekWorks: report.seekRecovered === true,
    zeroSeekWorks: report.zeroSeekVerified === true,
    actual60Hz: hz >= 58 && hz <= 62,
    frameBudget: frameP95 !== null && frameP95 <= 20 && !report.frames.some((ms) => ms >= 100),
    inputBudget: report.interactions.length >= 20 && inputP95 !== null && inputP95 <= 100,
    pointerBudget: report.positions.length >= report.seconds * 4 && errorMax <= 50,
    singleFlight: (report.maxInFlight.playback_status ?? 0) === 1 && (report.maxInFlight.analysis_spectrum ?? 0) <= 1 && report.rpcFailures.length === 0,
    resourceChurn: !!before && !!after && Number.isFinite(heap(before)) && Number.isFinite(heap(after)) && after.dom.jsEventListeners <= before.dom.jsEventListeners + 2 && after.dom.nodes <= before.dom.nodes + 20 && heap(after) <= heap(before) * 1.2 + 2 * 1024 * 1024,
  };
  return { accepted: Object.values(checks).every(Boolean), checks, measuredHz: hz, frameP95Ms: frameP95, frameWorstMs: report.frames.length ? report.frames.reduce((max,value)=>Math.max(max,value),0) : null, inputP95Ms: inputP95, pointerMaxWithUncertaintyMs: Number.isFinite(errorMax) ? errorMax : null, pointerSamples: report.positions.length };
}
