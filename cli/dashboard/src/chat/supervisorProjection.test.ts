import { describe, expect, test } from "bun:test";
import { initStreamState } from "../daemon/streamReducer.js";
import { reduceSupervisorEvent } from "./supervisorProjection.js";

describe("supervisor task-tree projection", () => {
  test("keeps B reasoning private but projects B tools and diffs into A", () => {
    let state = reduceSupervisorEvent(initStreamState(), {
      event: "reasoning_delta", process_id: "b", delta: "private reasoning",
    }, "a");
    state = reduceSupervisorEvent(state, {
      event: "toolcall_start", process_id: "b", tool_call_id: "write", tool_name: "write_file",
    }, "a");
    state = reduceSupervisorEvent(state, {
      event: "tool_result", process_id: "b", tool_call_id: "write", tool_name: "write_file",
      is_error: false, result_preview: "saved", diff: "--- a/x\n+++ b/x\n+ok\n",
    }, "a");
    expect(state.reasoning).toBe("");
    expect(state.activity.map(item => item.kind)).toEqual(["tool"]);
    expect(state.tools[0]).toMatchObject({actor_label: "Subprocess", process_id: "b"});
    expect(state.tools[0]?.diff).toContain("+++ b/x");
  });

  test("a child tool cannot clear A's response draft", () => {
    let state = reduceSupervisorEvent(initStreamState(), {
      event: "text_delta", process_id: "a", delta: "A draft", visibility: "public",
    }, "a");
    state = reduceSupervisorEvent(state, {
      event: "toolcall_start", process_id: "b", tool_call_id: "read", tool_name: "read_file",
    }, "a");
    expect(state.text).toBe("A draft");
  });
});
