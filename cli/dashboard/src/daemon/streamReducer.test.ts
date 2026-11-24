import { describe, expect, test } from "bun:test";
import { finalizeTools, initStreamState, reduceStreamEvent } from "./streamReducer.js";
import type {StreamEvent} from "./streamEvents.js";
import {timelineRows} from "../chat/timelineRows.js";
import {toolArgumentSummary} from "../chat/toolResult.js";

describe("native stream reducer", () => {
  test("tool-context preparation is hidden while the refined real invocation remains visible", () => {
    let state=initStreamState();
    for(const id of ["inspect","execute"]){
      state=reduceStreamEvent(state,{event:"toolcall_start",process_id:"p1",tool_name:"read_file",tool_call_id:id});
      state=reduceStreamEvent(state,id==="inspect"
        ?{event:"tool_context_prepared",process_id:"p1",tool_name:"read_file",tool_call_id:id,executed:false}
        :{event:"tool_result",process_id:"p1",tool_name:"read_file",tool_call_id:id,is_error:false,result_preview:"original body"});
    }
    expect(state.tools[0]).toMatchObject({context_only:true,is_running:false});
    expect(state.tools[1]).toMatchObject({state:"completed",result_text:"original body"});
    expect(timelineRows(state.activity,state.tools).flatMap(row=>row.toolIndices||[])).toEqual([1]);
    const unrelated=reduceStreamEvent(state,{event:"tool_context_prepared",process_id:"other",tool_name:"read_file",tool_call_id:"execute",executed:false});
    expect(unrelated).toBe(state);
  });
  test("the completed argument preview drives the concise tool line before any detail read",()=>{
    let state=reduceStreamEvent(initStreamState(),{event:"toolcall_start",process_id:"p1",tool_name:"read_file",tool_call_id:"read"});
    state=reduceStreamEvent(state,{event:"toolcall_done",process_id:"p1",tool_name:"read_file",tool_call_id:"read",
      argument_preview:{path:"src/中文.ts",offset:10,limit:20},detail_ref:"original-args"});
    expect(toolArgumentSummary(state.tools[0]!)).toBe("src/中文.ts:11–30");
    expect(state.tools[0]?.arguments).toBeUndefined();
  });
  test("keeps truncated public fragments together through review without duplicate progress", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "text_delta", delta: "Modules and data", process_id: "p1", step: 1, visibility: "public",
    });
    state = reduceStreamEvent(state, {
      event: "provider_request_started", process_id: "p1", step: 2,
      task_kind: "answer", answer_continuation: true,
    });
    state = reduceStreamEvent(state, {
      event: "text_delta", delta: " model. Tests.", process_id: "p1", step: 2,
    });
    state = reduceStreamEvent(state, {
      event: "provider_request_started", process_id: "p1", phase: "alignment", step: 3,
    });
    expect(state.text).toBe("Modules and data model. Tests.");
    expect(state.providerStep).toBe(2);
    expect(state.activity.filter(item => item.kind === "progress")).toHaveLength(0);
    // A real tool still ends a prose draft, even after a continuation.
    state = reduceStreamEvent(state, {
      event: "toolcall_start", process_id: "p1", tool_name: "read_file",
    });
    expect(state.text).toBe("");
    expect(state.activity.find(item => item.kind === "progress")?.text).toBe("Modules and data model. Tests.");
  });
  test("keeps prior state immutable and uses event time", () => {
    const initial = initStreamState();
    const next = reduceStreamEvent(initial, {
      event: "toolcall_start",
      process_id: "p1",
      tool_call_id: "call-1",
      tool_name: "read_file",
      time: "2026-08-25T00:00:00Z",
    });
    expect(initial.toolIndex.size).toBe(0);
    expect(next.toolIndex).not.toBe(initial.toolIndex);
    expect(next.tools[0]?.timestamp).toBe("2026-08-25T00:00:00Z");
  });

  test("associates same-name parallel results by call id", () => {
    let state = initStreamState();
    for (const id of ["call-1", "call-2"]) {
      state = reduceStreamEvent(state, {
        event: "toolcall_start", process_id: "p1",
        tool_call_id: id, tool_name: "read_file",
      });
    }
    state = reduceStreamEvent(state, {
      event: "tool_result", process_id: "p1", tool_call_id: "call-1",
      tool_name: "read_file", is_error: false, result_preview: "first",
    });
    expect(state.tools[0]?.result_text).toBe("first");
    expect(state.tools[1]?.is_running).toBe(true);
  });

  test("finalization cannot leave running cards behind", () => {
    const finalized = finalizeTools([{
      tool_name: "shell", target: "", allowed: true, status_label: "OK",
      duration_ms: 0, timestamp: "", is_running: true, state: "running",
    }]);
    expect(finalized[0]?.is_running).toBe(false);
    expect(finalized[0]?.state).toBe("unresolved");
    expect(finalized[0]?.allowed).toBe(false);
  });

  test("keeps a committed workspace diff on the tool card", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "toolcall_start", process_id: "p1",
      tool_call_id: "call-write", tool_name: "write_file",
    });
    state = reduceStreamEvent(state, {
      event: "tool_result", process_id: "p1", tool_call_id: "call-write",
      tool_name: "write_file", is_error: false,
      result_preview: "created index.html", diff: "--- a/index.html\n+++ b/index.html\n+hello\n",
    });
    expect(state.tools[0]?.diff).toContain("+++ b/index.html");
  });

  test("starts a fresh visible text buffer for every provider turn", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "provider_request_started", process_id: "p1", step: 1,
      task_kind: "answer",
    });
    state = reduceStreamEvent(state, {
      event: "text_delta", process_id: "p1", step: 1,
      visibility: "public", delta: "first answer",
    });
    state = reduceStreamEvent(state, {
      event: "provider_request_started", process_id: "p1", step: 2,
      task_kind: "supervisor",
    });
    state = reduceStreamEvent(state, {
      event: "text_delta", process_id: "p1", step: 2,
      visibility: "verbose", delta: "internal follow-up",
    });
    expect(state.text).toBe("internal follow-up");
    expect(state.text).not.toContain("first answer");
    expect(state.visibility).toBe("verbose");
    expect(state.providerStep).toBe(2);
    expect(state.activity.some(item => item.kind === "progress"
      && item.text === "first answer" && item.visibility === "public")).toBe(true);
  });

  test("a tool boundary preserves verbose prose visibility and reasoning order", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "reasoning_delta", process_id: "p1", delta: "checking constraints",
    });
    state = reduceStreamEvent(state, {
      event: "text_delta", process_id: "p1", delta: "I will inspect the workspace.",
      visibility: "verbose",
    });
    state = reduceStreamEvent(state, {
      event: "toolcall_start", process_id: "p1", tool_call_id: "read-1", tool_name: "read_file",
    });
    expect(state.text).toBe("");
    expect(state.activity.map(item => item.kind)).toEqual(["reasoning", "progress", "tool"]);
    expect(state.activity[1]).toMatchObject({
      kind: "progress", text: "I will inspect the workspace.", visibility: "verbose",
    });
  });

  test("does not project the legacy provider preparation template as Agent progress", () => {
    const state = reduceStreamEvent(initStreamState(), {
      event: "progress_summary", process_id: "p1", phase: "provider",
      message: "Understanding the request and preparing the next action.",
    });
    expect(state.activity).toEqual([]);
  });

  test("renders a durable orphan tool result instead of losing its diff", () => {
    const state = reduceStreamEvent(initStreamState(), {
      event: "tool_result", process_id: "b1", tool_call_id: "write-1",
      tool_name: "write_file", is_error: false, result_preview: "saved",
      diff: "--- a/a.ts\n+++ b/a.ts\n+const value = 1;\n",
    });
    expect(state.activity).toEqual([{kind: "tool", toolIndex: 0, detail_ref: undefined}]);
    expect(state.tools[0]).toMatchObject({process_id: "b1", state: "completed"});
    expect(state.tools[0]?.diff).toContain("+++ b/a.ts");
  });

  test("namespaces identical tool call ids by agent", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "toolcall_start", process_id: "a", tool_call_id: "call-1", tool_name: "read_file",
    });
    state = reduceStreamEvent(state, {
      event: "toolcall_start", process_id: "b", tool_call_id: "call-1", tool_name: "read_file",
    }, {preservePendingText: true});
    state = reduceStreamEvent(state, {
      event: "tool_result", process_id: "b", tool_call_id: "call-1",
      tool_name: "read_file", is_error: false, result_preview: "B result",
    });
    expect(state.tools[0]?.is_running).toBe(true);
    expect(state.tools[1]?.result_text).toBe("B result");
  });

  test("adds one completed card for each composite component receipt", () => {
    const event = {
      event: "composite_step_result" as const,
      process_id: "p1",
      execution_id: "execution-1",
      composite_tool: "inspect_runtime_contract",
      step_id: "find_contract",
      tool_name: "search_text",
      is_error: false,
      duration_ms: 12,
      receipt: { receipt_id: "receipt-1", committed: true },
    };
    const first = reduceStreamEvent(initStreamState(), event);
    const duplicate = reduceStreamEvent(first, event);
    expect(first.tools).toHaveLength(1);
    expect(first.tools[0]?.target).toBe("inspect_runtime_contract / find_contract");
    expect(first.tools[0]?.result_text).toContain("receipt-1");
    expect(first.tools[0]?.is_running).toBe(false);
    expect(duplicate.tools).toHaveLength(1);
  });

  test("keeps routine governance out of chat but surfaces a rejected completion", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "governance_snapshot", process_id: "p1",
      governance_version: 4, signal_count: 2, phase: "admission",
    });
    state = reduceStreamEvent(state, {
      event: "provider_usage", process_id: "p1",
      usage: { input_tokens: 100, output_tokens: 20 },
      cache: { eligible_input_tokens: 100, eligible_hit_ratio: 0.75 },
    });
    const accepted = {
      event: "completion_gate" as const, process_id: "p1",
      accepted: true, source: "complete_supervision",
    };
    state = reduceStreamEvent(state, accepted);
    state = reduceStreamEvent(state, accepted);
    expect(state.notices).toHaveLength(0);
    state = reduceStreamEvent(state, {
      event: "completion_gate", process_id: "p1", accepted: false,
      source: "complete_task", reason: "required test is missing",
    });
    expect(state.notices.map((item) => item.kind)).toEqual(["completion_gate"]);
    expect(state.providerUsage).toEqual({input_tokens: 100, output_tokens: 20});
    expect(state.notices[0]?.severity).toBe("warning");
  });

  test("surfaces hosted search fallback instead of silently changing transports", () => {
    const state = reduceStreamEvent(initStreamState(), {
      event: "provider_capability_fallback", process_id: "p1",
      capability: "hosted_web_search", from: "provider", to: "searxng",
      status_code: 400, reason: "provider_rejected_hosted_tool",
    });
    expect(state.notices).toHaveLength(1);
    expect(state.notices[0]).toMatchObject({
      label: "Provider capability fallback",
      severity: "warning",
    });
    expect(state.notices[0]?.detail).toContain("provider → searxng");
    expect(state.notices[0]?.detail).toContain("HTTP 400");
  });

  test("provider failover uses the existing notices without repeating ordinary requests", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "provider_route_selected", provider_id: "main", attempt: 1,
    });
    expect(state.notices).toHaveLength(0);
    state = reduceStreamEvent(state, {event: "provider_route_failed", provider_id: "main", reason: "HTTP_401"});
    state = reduceStreamEvent(state, {event: "provider_route_selected", provider_id: "backup", attempt: 2, message: "Using DeepSeek"});
    expect(state.notices.map(n => n.label)).toEqual(["Provider failed"]);
    state = reduceStreamEvent(state, {event: "provider_route_activated", provider_id: "backup", route_changed: true, message: "Using DeepSeek"});
    expect(state.notices.map(n => n.label)).toEqual(["Provider failed", "Provider switched"]);
    expect(state.notices[1]?.detail).toContain("DeepSeek");
    state = reduceStreamEvent(state, {event: "provider_route_skipped", provider_id: "main", reason: "circuit_unavailable"});
    state = reduceStreamEvent(state, {event: "provider_route_activated", provider_id: "backup", route_changed: false, message: "Using DeepSeek"});
    expect(state.notices).toHaveLength(2);
    state = reduceStreamEvent(state, {event: "provider_route_activated", provider_id: "main", route_changed: true, message: "Using Main"});
    expect(state.notices).toHaveLength(3); // actual recovery remains visible
  });

  test("surfaces storage degradation through the existing runtime notice path", () => {
    const state = reduceStreamEvent(initStreamState(), {
      event: "storage_health",
      storage: {
        level: "degraded",
        reasons: ["observability_write_throttled"],
      },
    });
    expect(state.notices).toHaveLength(1);
    expect(state.notices[0]?.label).toBe("Storage degraded");
    expect(state.notices[0]?.severity).toBe("warning");
    expect(state.notices[0]?.detail).toContain("observability_write_throttled");
  });

  test("surfaces DAG and worktree lifecycle through the formal runtime trajectory", () => {
    let state = reduceStreamEvent(initStreamState(), {
      event: "agent_dag_admitted", process_id: "a1",
      nodes: { api: "b1", tests: "b2" }, order: ["api", "tests"],
    });
    state = reduceStreamEvent(state, {
      event: "worktree_leased", process_id: "b1",
      worktree: { path: "C:/state/worktrees/b1" },
    });
    state = reduceStreamEvent(state, {
      event: "worktree_sealed", process_id: "b1",
      worktree: { result_commit: "abc123" },
    });
    expect(state.notices.map((item) => item.label)).toEqual([
      "Process DAG admitted", "Worktree leased", "Worktree sealed",
    ]);
    expect(state.notices[0]?.detail).toContain("api → tests");
    expect(state.notices[2]?.severity).toBe("success");
  });
});


test("separate proposal notices survive deduplication while no-knowledge completion stays quiet", () => {
  let state = initStreamState();
  for (const id of ["first", "second", "first"]) {
    state = reduceStreamEvent(state, {event: "lesson_harvest_proposed", proposal: {proposal_id: id}, count: 1});
  }
  expect(state.notices).toHaveLength(2);
  state = reduceStreamEvent(state, {event: "lesson_harvest_completed", count: 0});
  expect(state.notices).toHaveLength(2);
});

test("thinking is transient across explanation, tools, provider completion and the next request",()=>{
  let state=reduceStreamEvent(initStreamState(),{event:"provider_request_started",process_id:"a"});
  expect(state.thinking).toBe(true);expect(state.thinkingText).toBe("");
  state=reduceStreamEvent(state,{event:"reasoning_delta",process_id:"a",delta:"PRIVATE A"});
  expect(state.thinkingText).toBe("PRIVATE A");
  state=reduceStreamEvent(state,{event:"progress_summary",process_id:"a",update_id:"u",message:"PUBLIC EXPLANATION"});
  expect(state.thinking).toBe(false);expect(state.thinkingText).toBe("");
  expect(state.activity.filter(item=>item.kind==="progress").map(item=>item.text)).toEqual(["PUBLIC EXPLANATION"]);
  state=reduceStreamEvent(state,{event:"provider_request_started",process_id:"a"});
  expect(state.thinkingText).toBe("");
  state=reduceStreamEvent(state,{event:"reasoning_delta",process_id:"a",delta:"PRIVATE B"});
  expect(state.thinkingText).toBe("PRIVATE B");
  state=reduceStreamEvent(state,{event:"toolcall_start",process_id:"a",tool_call_id:"r",tool_name:"read_file"});
  expect(state.thinking).toBe(false);expect(state.thinkingText).toBe("");
  expect(state.reasoning).toBe("PRIVATE APRIVATE B"); // original evidence is intact
});

test('public reports bind the actual current process cycle detail without replay or child contamination',()=>{
 let state=reduceStreamEvent(initStreamState(),{event:'provider_request_started',process_id:'a'});
 state=reduceStreamEvent(state,{event:'reasoning_delta',process_id:'a',delta:'same cycle detail'});
 const first=state.stage!;
 state=reduceStreamEvent(state,{event:'progress_delta',process_id:'a',update_id:'report',delta:'Public '});
 state=reduceStreamEvent(state,{event:'progress_summary',process_id:'a',update_id:'report',message:'Public report'});
 expect(state.thinking).toBe(false);expect(state.stage?.reasoning).toBe('same cycle detail');
 const reports=state.activity.filter(i=>i.kind==='progress');expect(reports).toHaveLength(1);expect(reports[0]?.stage_id).toBe(first.id);
 const withChild=reduceStreamEvent(state,{event:'progress_summary',process_id:'b',update_id:'child',message:'Child report'},{preservePendingText:true});
 expect(withChild.stage).toBe(state.stage);expect(withChild.activity.at(-1)?.stage_id).toBeUndefined();
 state=reduceStreamEvent(state,{event:'provider_request_started',process_id:'a'});
 expect(state.stage?.id).not.toBe(first.id);expect(state.stage?.reasoning).toBe('');
 state=reduceStreamEvent(state,{event:'reasoning_delta',process_id:'a',delta:'new detail'});
 state=reduceStreamEvent(state,{event:'decision_required',process_id:'a',decision:{decision_id:'d'}} as any);
 expect(state.stage).toBeUndefined();
});

for(const event of [
  {event:"text_delta",delta:"Public text",visibility:"public"},
  {event:"progress_delta",delta:"Public stage",update_id:"u"},
  {event:"provider_response_completed"},
  {event:"decision_required",decision:{decision_id:"d"}},
  {event:"agent_terminal",status:"failed"},
  {event:"agent_complete",status:"completed"},
  {event:"stream_recovery"},
] as StreamEvent[])test(`thinking stops at ${event.event} without requiring more reasoning tokens`,()=>{
  const thinking=reduceStreamEvent(initStreamState(),{event:"reasoning_delta",process_id:"a",delta:"PRIVATE"});
  const next=reduceStreamEvent(thinking,event);
  expect(next.thinking).toBe(false);expect(next.thinkingText).toBe("");
});

test("partial DAG admission failure preserves already-started work in its notice",()=>{
 const state=reduceStreamEvent(initStreamState(),{event:"agent_dag_admission_failed",process_id:"a",
  execution_state:"partially_started",started_process_ids:["b"],cancelled_before_start_process_ids:["c"],error:"start failed"});
 expect(state.notices[0]?.severity).toBe("error");
 expect(state.notices[0]?.detail).toContain("1 started");
 expect(state.notices[0]?.detail).toContain("1 cancelled before start");
 expect(state.notices[0]?.detail).not.toContain("rolled back");
});
