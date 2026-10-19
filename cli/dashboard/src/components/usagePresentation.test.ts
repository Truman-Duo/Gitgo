import { test, expect } from "bun:test";
import {
  waterfallRows,
  waterfallBar,
  heatCalendar,
  tokenTotal,
} from "./usagePresentation.js";
import { useSelectionStyle } from "../theme/useSelectionStyle.js";
import { colors } from "../theme/index.js";
const event = (
  seq: number,
  event: string,
  process_id: string,
  ms: number,
  extra: any = {},
) => ({
  seq,
  event,
  process_id,
  time: new Date(Date.UTC(2026, 9, 17) + ms).toISOString(),
  ...extra,
});
test("waterfall pairs concurrent processes independently and keeps incomplete timings unknown", () => {
  const rows = waterfallRows([
    event(1, "provider_request_started", "a", 0, { step: 1 }),
    event(2, "provider_request_started", "b", 100, { step: 1 }),
    event(3, "provider_usage", "a", 900, {
      input_tokens: 100,
      output_tokens: 20,
    }),
    event(4, "provider_response_completed", "b", 1100, {
      duration_ms: 1000,
      completed_step: 1,
    }),
    event(5, "provider_response_completed", "a", 2000, {
      duration_ms: 2000,
      completed_step: 1,
    }),
    event(6, "tool_executed", "a", 2600, {
      duration_ms: 600,
      tool_name: "read_file",
      tool_call_id: "call",
    }),
    event(7, "tool_executed", "a", 2600, {
      duration_ms: 600,
      tool_name: "read_file",
      tool_call_id: "call",
    }),
    event(8, "provider_request_started", "a", 3000, { step: 2 }),
  ]);
  expect(rows).toHaveLength(4);
  expect(rows[0]).toMatchObject({
    duration: 2000,
    input: 100,
    output: 20,
    status: "complete",
  });
  expect(rows[1]).toMatchObject({ process: "b", duration: 1000, input: null });
  expect(rows[3]).toMatchObject({
    round: 2,
    duration: null,
    status: "no completion record",
  });
  expect(waterfallBar(rows[3]!, rows, 20)).toBe(null);
  expect(waterfallBar(rows[2]!, rows, 20)!.offset).toBeGreaterThan(0);
});
test("retained completions without starts use explicit duration; no zero timing is invented", () => {
  const rows = waterfallRows([
    event(1, "provider_response_incomplete", "a", 1200, {
      duration_ms: 1200,
      completed_step: 4,
    }),
    event(2, "provider_request_started", "b", 1500, { step: 1 }),
  ]);
  expect(rows[0]).toMatchObject({
    round: 4,
    duration: 1200,
    status: "incomplete",
  });
  expect(rows[0]!.start).toBe(Date.UTC(2026, 9, 17));
  expect(rows[1]!.duration).toBe(null);
});
test("daily calendar has unique leap-day cells, no cache double count and separates future dates", () => {
  const daily = [
    {
      date: "2024-02-29",
      input_tokens: 100,
      output_tokens: 20,
      cache_read_tokens: 80,
      provider_calls: 3,
    },
  ];
  const cells = heatCalendar(daily, "2024-03-01", 4).flat();
  expect(new Set(cells.map((c) => c.date)).size).toBe(28);
  expect(cells.find((c) => c.date === "2024-02-29")!.value).toBe(120);
  expect(tokenTotal(daily[0])).toBe(120);
  expect(cells.filter((c) => c.future).map((c) => c.date)).toEqual([
    "2024-03-02",
    "2024-03-03",
  ]);
  expect(
    heatCalendar(daily, "2024-03-01", 4, "requests")
      .flat()
      .find((c) => c.date === "2024-02-29")!.value,
  ).toBe(3);
});
test("nonfocused block uses the same high contrast foreground as the root tab", () => {
  expect(useSelectionStyle("non-focused", "block")).toMatchObject({
    bg: colors.selection.dim.block.bg,
    fg: colors.selection.block.fg,
    bold: false,
  });
});
