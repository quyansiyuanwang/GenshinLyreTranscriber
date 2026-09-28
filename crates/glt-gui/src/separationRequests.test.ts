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
