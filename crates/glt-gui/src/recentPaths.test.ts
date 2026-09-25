import { describe, expect, it } from "vitest";

import { addRecentPath, parseRecentPaths } from "./recentPaths";

describe("recent paths", () => {
  it("deduplicates, promotes and limits paths", () => {
    let paths: string[] = [];
    for (let index = 0; index < 10; index += 1) paths = addRecentPath(paths, `C:/media/${index}.wav`);
    expect(paths).toHaveLength(8);
    expect(paths[0]).toBe("C:/media/9.wav");
    paths = addRecentPath(paths, "C:/media/5.wav");
    expect(paths[0]).toBe("C:/media/5.wav");
    expect(paths).toHaveLength(8);
  });

  it("rejects invalid persisted values", () => {
    expect(parseRecentPaths("not-json")).toEqual([]);
    expect(parseRecentPaths('["a",2,null]')).toEqual(["a"]);
  });
});
