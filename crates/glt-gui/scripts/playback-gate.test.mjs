import { test } from "vitest";
import assert from "node:assert/strict";
import { percentile, summarizeGate } from "./playback-gate-metrics.mjs";
function report() {
  const resource = { dom: { nodes: 200, jsEventListeners: 30 }, metrics: [{ name: "JSHeapUsedSize", value: 10_000_000 }] };
  return { zeroSeekVerified: true, seekRecovered: true, measuredSteadySeconds: 610, continuityChecks: 11000, discontinuities: [], seconds: 610, frames: Array(36600).fill(1000 / 60), interactions: Array(25).fill({ms:30}), positions:Array(2500).fill({error_ms:5,uncertainty_ms:1}), resources:[{phase:'before-churn',...resource},{phase:'after-churn',...resource}], sourceDurationUs:600_430_000,firstPositionUs:10000,lastPositionUs:600_430_000,requestedStarts:[0],maxInFlight:{playback_status:1},rpcFailures:[] };
}
test('complete independent criteria accept a valid run',()=>assert.equal(summarizeGate(report()).accepted,true));
test('180Hz is never reported as a passing 60Hz run',()=>{const r=report();r.frames.fill(1000/180);assert.equal(summarizeGate(r).checks.actual60Hz,false);assert.equal(summarizeGate(r).accepted,false)});
test('short measurements, invisible pointers and request overlap fail',()=>{const r=report();r.seconds=60;r.positions=[];r.maxInFlight.playback_status=2;const g=summarizeGate(r);assert.equal(g.checks.fullTenMinuteFile,false);assert.equal(g.checks.pointerBudget,false);assert.equal(g.checks.singleFlight,false)});
test('clock uncertainty and listener growth are counted',()=>{const r=report();r.positions[0]={error_ms:49,uncertainty_ms:3};r.resources[1]={...r.resources[1],dom:{nodes:200,jsEventListeners:40}};assert.equal(summarizeGate(r).checks.pointerBudget,false);assert.equal(summarizeGate(r).checks.resourceChurn,false)});
test('percentiles handle empty input and long stalls remain visible',()=>{assert.equal(percentile([],0.95),null);const r=report();r.frames.push(120);assert.equal(summarizeGate(r).checks.frameBudget,false)});


test("declared duration or a seek to the end cannot impersonate full playback",()=>{const r=report();r.measuredSteadySeconds=2;assert.equal(summarizeGate(r).checks.fullTenMinuteFile,false);r.measuredSteadySeconds=610;r.discontinuities=[20000];assert.equal(summarizeGate(r).checks.fullTenMinuteFile,false)});
