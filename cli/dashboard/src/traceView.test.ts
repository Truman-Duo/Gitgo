import { describe, expect, test } from "bun:test";
import { mergeTraceEvents, verboseEventLines } from "./traceView.js";

describe("trace live view", () => {
  test("merges polling pages by monotonic sequence without duplicates", () => {
    const base = [{ seq: 1, time: "", event: "agent_started" }];
    const next = mergeTraceEvents(base, [
      { seq: 1, time: "", event: "agent_started" },
      { seq: 3, time: "", event: "text_delta", delta: "three" },
      { seq: 2, time: "", event: "reasoning_delta", delta: "two" },
    ]);
    expect(next.map(item => item.seq)).toEqual([1, 2, 3]);
  });

  test("verbose view preserves full reasoning instead of tail truncation", () => {
    const reasoning = "first-" + "x".repeat(240) + "-last";
    const lines = verboseEventLines({
      seq: 4, time: "2026-08-25T00:00:00Z", event: "reasoning_delta",
      process_id: "process-123", delta: reasoning,
    }, 50);
    expect(lines.join("\n")).toContain("first-");
    expect(lines.join("\n")).toContain("-last");
    expect(lines[0]).toContain("THINK reasoning_delta");
  });

  test("provider requests expose the content-addressed prompt snapshot", () => {
    const lines = verboseEventLines({
      seq: 5, time: "", event: "provider_request_started", step: 2,
      detail_ref: "trace-object:abc",
    }, 100);
    expect(lines.join("\n")).toContain("trace-object:abc");
  });

  test("composite steps expose component and receipt lineage", () => {
    const lines = verboseEventLines({
      seq: 6, time: "", event: "composite_step_result",
      composite_tool: "inspect_runtime_contract", step_id: "find_contract",
      tool_name: "search_text", is_error: false,
      receipt: { receipt_id: "receipt-1", committed: true },
    }, 100);
    expect(lines.join("\n")).toContain("inspect_runtime_contract");
    expect(lines.join("\n")).toContain("receipt-1");
  });
});
