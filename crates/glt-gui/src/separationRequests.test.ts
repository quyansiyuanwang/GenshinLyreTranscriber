import { describe, expect, it } from "vitest";
import fixture from "../../../tests/fixtures/desktop-separation-v1.json";
import { routingArgs, separationArgs } from "./separationRequests";

describe("desktop separation wire contract", () => {
  it("sends snake_case fields inside the separation request", () => {
    expect(separationArgs({ component: "component", input: "song.flac", output: "stems", model: "htdemucs", worker_path: null }))
      .toEqual({ request: fixture.separation });
  });
  it("sends snake_case fields inside the routing request", () => {
    expect(routingArgs({ stem_set: "stems/stem-set.json", output: "routing", mode: "solo", max_voices: 3, plan: null, worker_path: null }))
      .toEqual({ request: fixture.routing });
  });
});

it("recognizes the versioned desktop cancellation event", async () => {
  const { default: event } = await import("../../../tests/fixtures/desktop-cancellation-v1.json");
  expect(event).toEqual({ format_version: 1, operation: "separation", state: "cancelled" });
});
