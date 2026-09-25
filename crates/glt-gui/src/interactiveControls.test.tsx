import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import FilterPreviewCanvas from "./FilterPreviewCanvas";
import { notesInMarquee } from "./PianoRollEditor";
import ParameterSlider from "./ParameterSlider";
import SegmentedControl from "./SegmentedControl";

describe("interactive controls", () => {
  it("renders segmented choices as accessible radio buttons", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        label="时序模式"
        value="auto"
        options={[
          { value: "auto", label: "AUTO", hint: "自动分析" },
          { value: "straight", label: "1/16", hint: "直拍" },
        ]}
        onChange={() => undefined}
      />,
    );
    expect(html).toContain('role="radiogroup"');
    expect(html).toContain('aria-checked="true"');
    expect(html).toContain('aria-checked="false"');
    expect(html).toContain("自动分析");
  });

  it("selects notes intersecting a piano-roll marquee", () => {
    const note = {
      id: "a",
      start_us: 200_000,
      end_us: 300_000,
      key: "A",
      pitch: 60,
      velocity: 90,
      confidence: 0.8,
      source_stem: "edit",
      candidate_id: null,
      original_pitch: 60,
      pitch_center: 60,
      pitch_bend_class: "stable" as const,
      pitch_bends: [],
    };
    expect(notesInMarquee([note], { x1: 0, y1: 0, x2: 1000, y2: 500 }, 0, 1_000_000, 1000)).toEqual([note]);
    expect(notesInMarquee([note], { x1: 0, y1: 0, x2: 100, y2: 500 }, 0, 1_000_000, 1000)).toEqual([]);
  });

  it("renders the candidate distribution canvas", () => {
    const html = renderToStaticMarkup(
      <FilterPreviewCanvas notes={[]} rules={[]} onPitchLineChange={() => undefined} />,
    );
    expect(html).toContain('候选音符筛选分布图');
    expect(html).toContain('当前阈值保留');
  });

  it("renders draggable and precise numeric controls", () => {
    const html = renderToStaticMarkup(
      <ParameterSlider
        label="最低置信度"
        minimum={0}
        maximum={1}
        step={0.01}
        value={null}
        fallback={0.2}
        precision={2}
        unsetLabel="跟随档位"
        onChange={() => undefined}
      />,
    );
    expect(html).toContain('type="range"');
    expect(html).toContain('type="number"');
    expect(html).toContain("跟随档位");
  });
});
