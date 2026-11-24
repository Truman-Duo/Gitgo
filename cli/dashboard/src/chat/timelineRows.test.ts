import {expect, test} from "bun:test";
import type {StreamActivity, ToolCallCard} from "../types.js";
import {timelineRows} from "./timelineRows.js";

const tool = (id: string, start = 0, patch: Partial<ToolCallCard> = {}): ToolCallCard => ({tool_name: "web_fetch", tool_call_id: id,
  process_id: "a", target: id, state: "completed", allowed: true, status_label: "OK", timestamp: new Date(1_700_000_000_000 + start).toISOString(), duration_ms: 100, ...patch});
const activity: StreamActivity[] = [0, 1, 2].map(toolIndex => ({kind: "tool", toolIndex}));
test("only proven overlapping successes from one source share a web summary", () => {
  expect(timelineRows(activity, [tool("a"), tool("b", 10), tool("c", 20)]).map(row => row.toolIndices)).toEqual([[0, 1, 2]]);
  for (const patch of [{state: "error" as const, allowed: false}, {state: "unresolved" as const}, {state: "pending" as const},
    {process_id: "b"}, {timestamp: ""}]) {
    expect(timelineRows(activity, [tool("a"), tool("b", 10, patch), tool("c", 20)]).map(row => row.toolIndices)).toEqual([[0], [1], [2]]);
  }
  expect(timelineRows(activity, [tool("a"), tool("b", 110), tool("c", 220)]).map(row => row.toolIndices)).toEqual([[0], [1], [2]]);
});
test("reasoning identity survives public projection and another process interleaving", () => {
  const first: StreamActivity = {kind: "reasoning", text: "first", process_id: "a"};
  const second: StreamActivity = {kind: "reasoning", text: "second", process_id: "a"};
  const rows = timelineRows([first, {kind: "progress", text: "public", visibility: "public", process_id: "a"}, second], []);
  const updated = timelineRows([first, {kind: "reasoning", text: "child", process_id: "b"}, second], []);
  expect(rows.filter(row => row.activity.kind === "reasoning").map(row => row.id)).toEqual(updated.filter(row => row.activity.kind === "reasoning" && row.activity.process_id === "a").map(row => row.id));
});
