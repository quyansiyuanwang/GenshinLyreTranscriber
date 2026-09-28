import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT, parseWorkspaceLayout } from "./workspaceLayout";

describe("workspace preferences", () => {
  it("falls back for missing, corrupt and future versions", () => {
    for (const text of [null, "{", "null", "[]", '{"version":2}', '"text"']) expect(parseWorkspaceLayout(text)).toEqual(DEFAULT_LAYOUT);
  });
  it("round-trips all views and collapsed dimensions", () => {
    for (const view of ["analysis", "filter", "editor"]) {
      const expected = { ...DEFAULT_LAYOUT, view, resources: false, inspector: false, tasks: true, resourceWidth: 350, inspectorWidth: 260, taskHeight: 280 };
      expect(parseWorkspaceLayout(JSON.stringify(expected))).toEqual(expected);
    }
  });
  it("clamps dimensions and rejects invalid flags instead of hiding the workspace", () => {
    expect(parseWorkspaceLayout(JSON.stringify({ version: 1, view: "missing", resources: "false", inspector: 0, tasks: 1, resourceWidth: -50, inspectorWidth: 5000, taskHeight: "NaN" })))
      .toEqual({ ...DEFAULT_LAYOUT, resourceWidth: 200, inspectorWidth: 420 });
  });
});
