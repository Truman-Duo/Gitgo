import { describe, expect, test } from "bun:test";
import { timelineFromTrace } from "./turnTimeline.js";
import {timelineRows} from "./timelineRows.js";
import {toolArgumentSummary} from "./toolResult.js";

describe("authoritative turn timeline", () => {
  test("projects immutable helper events and attributes a parent promotion to its producers", () => {
    const model = timelineFromTrace([
      {seq:1,time:"",event:"agent_started",process_id:"helper",actor_kind:"worker",temporary_subagent:true},
      {seq:2,time:"",event:"toolcall_start",process_id:"helper",tool_name:"read_file",temporary_subagent:true},
      {seq:3,time:"",event:"toolcall_start",process_id:"worker",tool_name:"read_file",role:"executor"},
      {seq:4,time:"",event:"agent_terminal",process_id:"helper",temporary_subagent:true,status:"failed"},
      {seq:5,time:"",event:"worktree_promoted",process_id:"a",promotion:{process_ids:["helper"]}},
      {seq:6,time:"",event:"worktree_promoted",process_id:"a",promotion:{process_ids:["worker"]}},
      {seq:7,time:"",event:"worktree_promoted",process_id:"a",promotion:{process_ids:["helper","worker"]}},
    ],"a");
    expect(model.tools.map(tool=>tool.actor_label)).toEqual(["Subagent","Subprocess"]);
    expect(model.notices.map(notice=>notice.label)).toEqual([
      "Subagent started","Subagent failed","Subagent changes applied","Subprocess changes promoted","Changes applied",
    ]);
    expect(model.notices[1]?.severity).toBe("warning");
    expect(model.notices[2]?.label_copy?.zh).toBe("子代理补丁已应用");
  });

  test("historical context preparation never impersonates execution or hides another actor",()=>{
    const model=timelineFromTrace([
      {seq:1,time:"",event:"toolcall_start",process_id:"a",tool_name:"read_file",tool_call_id:"inspect"},
      {seq:2,time:"",event:"tool_context_prepared",process_id:"a",tool_name:"read_file",tool_call_id:"inspect",executed:false},
      {seq:3,time:"",event:"toolcall_start",process_id:"b",tool_name:"read_file",tool_call_id:"inspect"},
      {seq:4,time:"",event:"tool_result",process_id:"b",tool_name:"read_file",tool_call_id:"inspect",is_error:false,result_preview:"child body"},
      {seq:5,time:"",event:"toolcall_start",process_id:"a",tool_name:"read_file",tool_call_id:"execute"},
      {seq:6,time:"",event:"tool_result",process_id:"a",tool_name:"read_file",tool_call_id:"execute",is_error:false,result_preview:"main body"},
    ],"a");
    expect(model.tools[0]?.context_only).toBe(true);
    expect(timelineRows(model.activity,model.tools).flatMap(row=>row.toolIndices||[])).toEqual([1,2]);
    expect(model.tools[1]?.result_text).toBe("child body");
    expect(model.tools[2]?.result_text).toBe("main body");
  });
  test("persisted completed-call headers retain concise parameters without a transient start",()=>{
    const model=timelineFromTrace([
      {seq:1,time:"",event:"toolcall_done",process_id:"a",tool_name:"read_file",tool_call_id:"read",
        argument_preview:{path:"src/中文.ts",offset:10,limit:20}},
      {seq:2,time:"",event:"tool_result",process_id:"a",tool_name:"read_file",tool_call_id:"read",is_error:false},
    ],"a");
    expect(model.tools).toHaveLength(1);
    expect(toolArgumentSummary(model.tools[0]!)).toBe("src/中文.ts:11–30");
  });
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
      "reasoning", "progress", "context", "reasoning", "tool", "notice",
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
