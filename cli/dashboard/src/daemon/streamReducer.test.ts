import { describe, expect, test } from "bun:test";
import { finalizeTools, initStreamState, reduceStreamEvent } from "./streamReducer.js";

describe("native stream reducer", () => {
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

  test("commits provider commentary before a tool and keeps reasoning ordered", () => {
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
      kind: "progress", text: "I will inspect the workspace.", visibility: "public",
    });
  });

  test("keeps deterministic phase summaries visible when reasoning is collapsed", () => {
    const state = reduceStreamEvent(initStreamState(), {
      event: "progress_summary", process_id: "p1", phase: "provider",
      message: "Understanding the request and preparing the next action.",
    });
    expect(state.activity).toEqual([{
      kind: "progress",
      text: "Understanding the request and preparing the next action.",
      visibility: "public", process_id: "p1", actor_label: undefined,
    }]);
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
