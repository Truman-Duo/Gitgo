import { describe, expect, test } from "bun:test";
import { getProcessStream, publishProcessStreamEvent } from "./processStreams.js";

describe("process-scoped streams", () => {
  test("keeps concurrent A and B text isolated", () => {
    const project = "stream-isolation-test";
    publishProcessStreamEvent(project, {
      event: "text_delta", process_id: "root-a", delta: "A says",
      visibility: "public",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent(project, {
      event: "reasoning_delta", process_id: "child-b", delta: "B thinks",
    }, "2026-01-01T00:00:01Z");
    publishProcessStreamEvent(project, {
      event: "text_delta", process_id: "child-b", delta: "B says",
      visibility: "public",
    }, "2026-01-01T00:00:01Z");

    expect(getProcessStream(project, "root-a")?.text).toBe("A says");
    expect(getProcessStream(project, "root-a")?.reasoning).toBe("");
    expect(getProcessStream(project, "child-b")?.text).toBe("B says");
    expect(getProcessStream(project, "child-b")?.reasoning).toBe("B thinks");
  });

  test("seals a B transient stream at agent_terminal", () => {
    const project = "stream-terminal-test";
    publishProcessStreamEvent(project, {
      event: "reasoning_delta", process_id: "child-terminal", delta: "still thinking",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent(project, {
      event: "agent_terminal", process_id: "child-terminal", status: "completed",
    }, "2026-01-01T00:00:01Z");
    const stream = getProcessStream(project, "child-terminal");
    expect(stream?.reasoning).toBe("");
    expect(stream?.notices.at(-1)?.label).toBe("Subprocess finished");
  });

  test("parks visible B progress when it asks the user instead of thinking forever", () => {
    publishProcessStreamEvent("project", {
      event: "reasoning_delta", process_id: "child-question", delta: "considering",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent("project", {
      event: "decision_required", process_id: "child-question",
      decision: {decision_id: "decision-1"},
    }, "2026-01-01T00:00:00Z");
    const row = getProcessStream("project", "child-question");
    expect(row?.reasoning).toBe("");
  });

  test("retains B reasoning, commentary, tool and diff in one ordered live timeline", () => {
    const project = "stream-timeline-test";
    const process = "child-timeline";
    publishProcessStreamEvent(project, {
      event: "reasoning_delta", process_id: process, delta: "inspect first",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent(project, {
      event: "text_delta", process_id: process, delta: "Reading the source.", visibility: "verbose",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent(project, {
      event: "toolcall_start", process_id: process, tool_call_id: "write", tool_name: "write_file",
    }, "2026-01-01T00:00:00Z");
    publishProcessStreamEvent(project, {
      event: "tool_result", process_id: process, tool_call_id: "write", tool_name: "write_file",
      is_error: false, result_preview: "saved", diff: "--- a/x\n+++ b/x\n+ok\n",
    }, "2026-01-01T00:00:00Z");
    const row = getProcessStream(project, process)!;
    expect(row.activity?.map(item => item.kind)).toEqual(["reasoning", "progress", "tool"]);
    expect(row.tools[0]?.diff).toContain("+++ b/x");
  });
});
