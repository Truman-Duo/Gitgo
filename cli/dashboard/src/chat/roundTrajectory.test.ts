import { describe, expect, test } from "bun:test";
import { compactEventLine, roundLabel, roundTraceEvents } from "./roundTrajectory.js";
import type { ChatMessage } from "../types.js";
import type { TraceRecord } from "../traceView.js";

describe("completed-round projection", () => {
  const msg = { role: "assistant", content: "answer", timestamp: "now" } as ChatMessage;
  test("never labels failure, cancellation or a decision as completed", () => {
    expect(roundLabel({ ...msg, status: "completed" })).toBe("Completed");
    expect(roundLabel({ ...msg, status: "awaiting_user" })).toBe("Awaiting input");
    for (const status of ["failed", "degraded", "cancelled", "timed_out", "running"]) {
      expect(roundLabel({ ...msg, status })).toBeNull();
    }
    expect(roundLabel({ ...msg, kind: "error", status: "completed" })).toBeNull();
  });
  test("saved failures expose their trace without claiming completion", () => {
    for (const status of ["failed", "degraded", "cancelled", "timed_out"]) {
      expect(roundLabel({ ...msg, kind: "error", status, trace_id: "trace" })).toBe("Failure details");
      expect(roundLabel({ ...msg, status, trace_id: "trace" })).toBe("Failure details");
    }
    expect(roundLabel({ ...msg, role: "user", status: "failed", trace_id: "trace" })).toBeNull();
    expect(roundLabel({ ...msg, kind: "error", status: "completed", trace_id: "trace" })).toBe("Failure details");
  });
  test("a B opens its events from the shared tree trace, without A or sibling reasoning", () => {
    const events = ["root", "B", "sibling"].map((process_id, seq) => ({
      seq, process_id, time: "now", event: "reasoning_delta", delta: process_id,
    }));
    expect(roundTraceEvents(events, "B").map(event => event.delta)).toEqual(["B"]);
  });
  test("compact mode keeps receipts and elapsed time but not reasoning", () => {
    const event = { seq: 1, time: "now", process_id: "B" };
    expect(compactEventLine({ ...event, event: "reasoning_delta", delta: "private" })).toBeNull();
    expect(compactEventLine({ ...event, event: "tool_result", tool_name: "read_file", duration_ms: 60_001 })).toBe("Used · read_file · 1min,1s");
    expect(compactEventLine({ ...event, event: "tool_result", tool_name: "edit_file", is_error: true, error: "GITGO-E1234", duration_ms: 1 } as TraceRecord)).toContain("Tool failed · edit_file · 0.01s · GITGO-E1234");
  });
});
