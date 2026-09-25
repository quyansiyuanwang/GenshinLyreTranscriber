import { describe, expect, it } from "vitest";

import { buildCustomRoutingPlan, defaultStemRoute } from "./routingPlan";

describe("custom routing plan", () => {
  it("builds a versioned plan from steram controls", () => {
    const plan = buildCustomRoutingPlan(
      ["vocals", "bass"],
      {
        vocals: { ...defaultStemRoute("vocals"), muted: true, gainDb: -8 },
        bass: { ...defaultStemRoute("bass"), solo: true, target: "melody" },
      },
      3,
    );
    expect(plan.mode).toBe("custom");
    expect(plan.max_voices).toBe(3);
    expect(plan.routes[0]).toMatchObject({ sources: ["vocals"], muted: true, gain_db: -8 });
    expect(plan.routes[1]).toMatchObject({ sources: ["bass"], solo: true, target: "melody" });
  });

  it("clamps gain and voice settings", () => {
    const plan = buildCustomRoutingPlan(
      ["other"],
      { other: { ...defaultStemRoute("other"), gainDb: 100 } },
      99,
    );
    expect(plan.max_voices).toBe(21);
    expect(plan.routes[0].gain_db).toBe(24);
  });
});
