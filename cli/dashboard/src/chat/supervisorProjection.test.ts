import { describe, expect, test } from "bun:test";
import { initStreamState } from "../daemon/streamReducer.js";
import { reduceSupervisorEvent } from "./supervisorProjection.js";

describe("supervisor task-tree projection", () => {
  test("shows a temporary helper distinctly from an ordinary subprocess", () => {
    const root = "a";
    let state = reduceSupervisorEvent(initStreamState(), {
      event:"agent_started",process_id:"helper",actor_kind:"worker",role:"subagent",
    }, root);
    state = reduceSupervisorEvent(state, {
      event:"toolcall_start",process_id:"helper",tool_name:"read_file",temporary_subagent:true,
    }, root);
    state = reduceSupervisorEvent(state, {
      event:"toolcall_start",process_id:"worker",tool_name:"read_file",role:"executor",
    }, root);
    state = reduceSupervisorEvent(state, {
      event:"agent_terminal",process_id:"helper",temporary_subagent:true,status:"completed",
    }, root);
    expect(state.tools.map(tool=>tool.actor_label)).toEqual(["Subagent","Subprocess"]);
    expect(state.notices.map(notice=>notice.label)).toEqual(["Subagent started","Subagent completed"]);
  });

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

  test("child explanation and completion cannot end A's current thinking",()=>{
    let state=reduceSupervisorEvent(initStreamState(),{event:"reasoning_delta",process_id:"a",delta:"A private"},"a");
    state=reduceSupervisorEvent(state,{event:"progress_summary",process_id:"b",update_id:"b:stage",message:"B public"},"a");
    state=reduceSupervisorEvent(state,{event:"provider_response_completed",process_id:"b"},"a");
    state=reduceSupervisorEvent(state,{event:"provider_request_parameters",process_id:"b",detail_ref:"B-input"},"a");
    state=reduceSupervisorEvent(state,{event:"agent_complete",process_id:"b",status:"completed"},"a");
    expect(state.thinking).toBe(true);expect(state.thinkingText).toBe("A private");
    expect(state.activity.filter(item=>item.kind==="context")).toHaveLength(0);
    state=reduceSupervisorEvent(state,{event:"progress_summary",process_id:"a",update_id:"a:stage",message:"A public"},"a");
    expect(state.thinking).toBe(false);expect(state.thinkingText).toBe("");
  });
});
