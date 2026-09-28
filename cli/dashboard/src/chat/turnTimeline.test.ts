import { describe, expect, test } from "bun:test";
import { timelineFromTrace } from "./turnTimeline.js";

describe("authoritative turn timeline", () => {
  test("keeps A/B events ordered, preserves diff and excludes final response duplication", () => {
    const model = timelineFromTrace([
      {seq: 1, time: "", event: "reasoning_delta", process_id: "a", delta: "A reasoning"},
      {seq: 2, time: "", event: "text_delta", process_id: "a", delta: "I will delegate."},
      {seq: 3, time: "", event: "provider_request_started", process_id: "a", step: 2},
      {seq: 4, time: "", event: "reasoning_delta", process_id: "b", delta: "B reasoning"},
      {seq: 5, time: "", event: "toolcall_start", process_id: "b", tool_call_id: "write", tool_name: "write_file"},
      {seq: 6, time: "", event: "tool_result", process_id: "b", tool_call_id: "write", tool_name: "write_file",
        is_error: false, result_preview: "saved", diff: "--- a/x\n+++ b/x\n+ok\n"},
      {seq: 7, time: "", event: "text_delta", process_id: "a", delta: "Final answer"},
      {seq: 8, time: "", event: "agent_complete", process_id: "a", status: "completed"},
    ], "a");
    expect(model.activity.map(item => item.kind)).toEqual([
      "reasoning", "progress", "reasoning", "tool", "notice",
    ]);
    expect(model.activity.some(item => item.kind === "progress" && item.text === "Final answer")).toBe(false);
    expect(model.tools[0]).toMatchObject({actor_label: "Subprocess", state: "completed"});
    expect(model.tools[0]?.diff).toContain("+++ b/x");
  });

  test("seals orphaned historical tools at the durable turn boundary", () => {
    const model = timelineFromTrace([
      {seq: 1, time: "2026-09-07T08:23:41Z", event: "toolcall_start", process_id: "a",
        tool_call_id: "complete", tool_name: "complete_supervision"},
      {seq: 2, time: "2026-09-07T08:23:42Z", event: "provider_usage", process_id: "a",
        usage: {input_tokens: 16486, output_tokens: 4901}},
    ], "a", {status: "completed", timestamp: "2026-09-07T08:25:34Z"});
    expect(model.tools[0]).toMatchObject({
      state: "unresolved", is_running: false, duration_ms: 113000,
    });
    expect(model.providerUsage).toEqual({input_tokens: 16486, output_tokens: 4901});
    expect(model.notices.some(item => item.kind === "provider_usage")).toBe(false);
  });
});
