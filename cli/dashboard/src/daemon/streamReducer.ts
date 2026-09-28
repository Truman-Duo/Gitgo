// src/daemon/streamReducer.ts — pure reducer for the native daemon token stream.
// Extracted from useChat's inline onChunk so the event→(text + tool cards)
// translation is testable and free of React/transport concerns. `toolIndex`
// maps tool_call_id and tool_name to an index in `tools` (two keys, one entry).
import type { ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import type { StreamEvent } from "./streamEvents.js";
import { runtimeNoticeFromEvent } from "./runtimeNotices.js";

export type StreamState = {
  text: string;
  reasoning: string;
  tools: ToolCallCard[];
  toolIndex: Map<string, number>;
  notices: RuntimeNotice[];
  activity: StreamActivity[];
  noticeKeys: Set<string>;
  visibility: "public" | "verbose";
  providerStep: number;
  providerUsage?: ProviderUsageSummary;
};

export function initStreamState(): StreamState {
  return {
    text: "", reasoning: "", tools: [], toolIndex: new Map(),
    notices: [], activity: [], noticeKeys: new Set(), visibility: "verbose", providerStep: 0,
  };
}

type ReduceOptions = {
  /** Child events projected into A must not commit or clear A's response draft. */
  preservePendingText?: boolean;
};

function eventActor(ev: StreamEvent): string | undefined {
  const actor = (ev as unknown as Record<string, unknown>).actor_label;
  return typeof actor === "string" && actor ? actor : undefined;
}

function toolKey(ev: StreamEvent, value: string): string {
  return `${String(ev.process_id || "host")}:${value}`;
}

function findToolIndex(state: StreamState, ev: StreamEvent): number | undefined {
  const id = "tool_call_id" in ev ? String(ev.tool_call_id || "") : "";
  const name = "tool_name" in ev ? String(ev.tool_name || "") : "";
  return (id ? state.toolIndex.get(toolKey(ev, id)) : undefined)
    ?? (name ? state.toolIndex.get(toolKey(ev, name)) : undefined);
}

/** Commit provider prose that preceded another step/tool as durable progress. */
function commitProgress(state: StreamState): StreamState {
  const text = state.text.trim();
  if (!text) return {...state, text: ""};
  return {
    ...state,
    text: "",
    activity: [...state.activity, {
      kind: "progress",
      text,
      // Provider-visible text that is followed by another tool/model step is
      // commentary, not the final answer. Keep it in compact mode just like
      // Codex/Claude Code progress cells; raw reasoning remains verbose-only.
      visibility: "public",
    }],
  };
}

function appendNotice(state: StreamState, ev: StreamEvent): StreamState {
  const notice = runtimeNoticeFromEvent(ev);
  if (!notice || state.noticeKeys.has(notice.key)) return state;
  const noticeKeys = new Set(state.noticeKeys);
  noticeKeys.add(notice.key);
  const notices = [...state.notices, notice];
  return {
    ...state, notices, noticeKeys,
    activity: [...state.activity, {
      kind: "notice", noticeIndex: notices.length - 1, detail_ref: notice.detail_ref,
    }],
  };
}

export function reduceStreamEvent(
  state: StreamState,
  ev: StreamEvent,
  options: ReduceOptions = {},
): StreamState {
  switch (ev.event) {
    case "text_delta":
      return {
        ...state,
        text: state.text + (ev.delta || ""),
        visibility: ev.visibility || state.visibility,
        providerStep: ev.step ?? state.providerStep,
      };

    case "reasoning_delta":
      {
        const delta = ev.delta || "";
        const activity = state.activity.slice();
        const last = activity[activity.length - 1];
        const actor = eventActor(ev);
        if (last?.kind === "reasoning"
            && last.process_id === ev.process_id && last.actor_label === actor) {
          activity[activity.length - 1] = {...last, text: last.text + delta};
        } else {
          activity.push({kind: "reasoning", text: delta, process_id: ev.process_id, actor_label: actor});
        }
        return {
          ...state,
          reasoning: state.reasoning + delta,
          activity,
          providerStep: ev.step ?? state.providerStep,
        };
      }

    case "progress_summary": {
      const message = String(ev.message || "").trim();
      if (!message) return state;
      const actor = eventActor(ev);
      const previous = state.activity[state.activity.length - 1];
      if (previous?.kind === "progress" && previous.text === message
          && previous.process_id === ev.process_id) return state;
      return {
        ...state,
        activity: [...state.activity, {
          kind: "progress", text: message, visibility: "public",
          process_id: ev.process_id, actor_label: actor,
        }],
      };
    }

    case "provider_usage": {
      const raw = (ev as unknown as {usage?: Record<string, unknown>}).usage || {};
      const next = {
        input_tokens: Number(raw.input_tokens || 0),
        output_tokens: Number(raw.output_tokens || 0),
      };
      return {
        ...state,
        providerUsage: {
          input_tokens: (state.providerUsage?.input_tokens || 0) + next.input_tokens,
          output_tokens: (state.providerUsage?.output_tokens || 0) + next.output_tokens,
        },
      };
    }

    case "provider_request_started":
      // Show the current provider turn. Previous turns remain in the durable
      // trace instead of being concatenated into one repeated chat response.
      {
        const committed = options.preservePendingText ? state : commitProgress(state);
        const noticed = appendNotice(committed, ev);
        return {
        ...noticed,
        text: options.preservePendingText ? state.text : "",
        visibility: ev.task_kind === "answer" ? "public" : "verbose",
        providerStep: ev.step ?? (state.providerStep + 1),
        };
      }

    case "toolcall_start": {
      const committed = options.preservePendingText ? state : commitProgress(state);
      const idx = committed.tools.length;
      const callId = ev.tool_call_id || ev.tool_name || "";
      const toolIndex = new Map(committed.toolIndex);
      if (callId) toolIndex.set(toolKey(ev, callId), idx);
      if (ev.tool_name) toolIndex.set(toolKey(ev, ev.tool_name), idx);
      const card: ToolCallCard = {
        tool_name: ev.tool_name || "",
        tool_call_id: callId,
        target: ev.target || "",
        allowed: true,
        status_label: "OK",
        duration_ms: 0,
        timestamp: ev.time || "",
        is_running: true,
        state: "running",
        actor_label: eventActor(ev),
        process_id: ev.process_id,
        detail_ref: ev.detail_ref,
      };
      return {
        ...committed, tools: [...committed.tools, card], toolIndex,
        activity: [...committed.activity, {kind: "tool", toolIndex: idx, detail_ref: ev.detail_ref}],
      };
    }

    case "toolcall_delta": {
      const idx = findToolIndex(state, ev);
      if (idx === undefined) return state;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        target: ((tools[idx].target || "") + (ev.delta || "")).slice(0, 500),
      };
      return { ...state, tools };
    }

    case "tool_result": {
      const existing = findToolIndex(state, ev);
      if (existing === undefined) {
        const idx = state.tools.length;
        const callId = ev.tool_call_id || ev.tool_name || "";
        const toolIndex = new Map(state.toolIndex);
        if (callId) toolIndex.set(toolKey(ev, callId), idx);
        if (ev.tool_name) toolIndex.set(toolKey(ev, ev.tool_name), idx);
        const card: ToolCallCard = {
          tool_name: ev.tool_name,
          tool_call_id: callId,
          target: "",
          result_text: ev.result_preview || ev.error || "",
          compact_summary: ev.compact_summary,
          diff: ev.diff,
          blocked_reason: ev.is_error ? (ev.error || "tool failed") : undefined,
          duration_ms: ev.duration_ms || 0,
          allowed: !ev.is_error,
          status_label: ev.is_error ? "ERROR" : "OK",
          timestamp: ev.time || "",
          is_running: false,
          state: ev.is_error ? "error" : "completed",
          actor_label: eventActor(ev),
          process_id: ev.process_id,
          detail_ref: ev.detail_ref,
        };
        return {
          ...state, tools: [...state.tools, card], toolIndex,
          activity: [...state.activity, {kind: "tool", toolIndex: idx, detail_ref: ev.detail_ref}],
        };
      }
      const idx = existing;
      const tools = state.tools.slice();
      tools[idx] = {
        ...tools[idx],
        result_text: ev.result_preview || ev.error || "",
        compact_summary: ev.compact_summary || tools[idx].compact_summary,
        diff: ev.diff || tools[idx].diff,
        blocked_reason: ev.is_error ? (ev.error || "tool failed") : undefined,
        duration_ms: ev.duration_ms || 0,
        allowed: !ev.is_error,
        is_running: false,
        state: ev.is_error ? "error" : "completed",
        detail_ref: ev.detail_ref || tools[idx].detail_ref,
      };
      return { ...state, tools };
    }

    case "composite_step_result": {
      const rawKey = `${ev.execution_id}:${ev.step_id}`;
      const key = toolKey(ev, rawKey);
      if (state.toolIndex.has(key)) return state;
      const idx = state.tools.length;
      const toolIndex = new Map(state.toolIndex);
      toolIndex.set(key, idx);
      const card: ToolCallCard = {
        tool_name: ev.tool_name,
        tool_call_id: rawKey,
        target: `${ev.composite_tool} / ${ev.step_id}`,
        result_text: ev.error || `receipt ${ev.receipt?.receipt_id || "recorded"}`,
        blocked_reason: ev.is_error ? (ev.error || "composite step failed") : undefined,
        duration_ms: ev.duration_ms || 0,
        allowed: !ev.is_error,
        status_label: ev.is_error ? "ERROR" : "OK",
        timestamp: ev.time || "",
        is_running: false,
        state: ev.is_error ? "error" : "completed",
        actor_label: eventActor(ev),
        process_id: ev.process_id,
      };
      return {
        ...state, tools: [...state.tools, card], toolIndex,
        activity: [...state.activity, {kind: "tool", toolIndex: idx}],
      };
    }

    case "tool_progress": {
      const idx = findToolIndex(state, ev);
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
        t.is_running ? {
          ...t, is_running: false, state: "error" as const, allowed: false,
          blocked_reason: "Stream interrupted before a durable tool result was received",
        } : t,
      );
      return appendNotice({ ...state, tools }, ev);
    }

    default:
      return appendNotice(state, ev);
  }
}

export function finalizeTools(tools: ToolCallCard[]): ToolCallCard[] {
  return tools.map((t) => ({
    ...t,
    is_running: false,
    state: !t.state || t.state === "running" ? "unresolved" : t.state,
    allowed: !t.state || t.state === "running" ? false : t.allowed,
    blocked_reason: !t.state || t.state === "running"
      ? (t.blocked_reason || "Tool result unavailable when the turn ended")
      : t.blocked_reason,
  }));
}
