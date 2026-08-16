// src/daemon/streamReducer.ts — pure reducer for the native daemon token stream.
// Extracted from useChat's inline onChunk so the event→(text + tool cards)
// translation is testable and free of React/transport concerns. `toolIndex`
// maps tool_call_id and tool_name to an index in `tools` (two keys, one entry).
import type { ToolCallCard } from "../types.js";
import type { StreamEvent } from "./streamEvents.js";

export type StreamState = {
  text: string;
  tools: ToolCallCard[];
  toolIndex: Map<string, number>;
};

export function initStreamState(): StreamState {
  return { text: "", tools: [], toolIndex: new Map() };
}

export function reduceStreamEvent(state: StreamState, ev: StreamEvent): StreamState {
  switch (ev.event) {
    case "text_delta":
      return { ...state, text: state.text + (ev.delta || "") };

    case "toolcall_start": {
      const idx = state.tools.length;
      const callId = ev.tool_call_id || ev.tool_name || "";
      state.toolIndex.set(callId, idx);
      if (ev.tool_name) state.toolIndex.set(ev.tool_name, idx);
      const card: ToolCallCard = {
        tool_name: ev.tool_name || "",
        tool_call_id: callId,
        target: ev.target || "",
        allowed: true,
        status_label: "OK",
        duration_ms: 0,
        timestamp: new Date().toISOString(),
        is_running: true,
        state: "running",
      };
      return { ...state, tools: [...state.tools, card] };
    }

    case "toolcall_delta": {
      const callId = ev.tool_call_id || ev.tool_name || "";
      const idx = state.toolIndex.get(callId);
      if (idx === undefined) return state;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        result_text: (tools[idx].result_text || "") + (ev.delta || ""),
      };
      return { ...state, tools };
    }

    case "tool_progress": {
      const idx = state.toolIndex.get(ev.tool_call_id || ev.tool_name || "");
      if (idx === undefined) return state;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        status_label: ev.status || tools[idx].status_label,
        allowed: ev.status !== "blocked",
        blocked_reason: ev.status === "blocked" ? ev.reason : undefined,
        is_running: ev.status === "running",
        state:
          ev.status === "blocked"
            ? "error"
            : ev.status === "running"
              ? "running"
              : "completed",
      };
      return { ...state, tools };
    }

    case "stream_recovery": {
      const tools = state.tools.map((t) =>
        t.is_running ? { ...t, is_running: false, state: "completed" as const } : t,
      );
      const text = `${state.text}\n[Stream interrupted — recovery attempt ${ev.attempt}/${ev.max}]`;
      return { ...state, text, tools };
    }
  }
}

export function finalizeTools(tools: ToolCallCard[]): ToolCallCard[] {
  return tools.map((t) => ({ ...t, is_running: false, state: t.state ?? "completed" }));
}
