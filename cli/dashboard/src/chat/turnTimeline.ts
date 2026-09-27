import type { ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { runtimeNoticeFromEvent } from "../daemon/runtimeNotices.js";
import type { TraceRecord } from "../traceView.js";

export type TurnTimelineModel = {
  activity: StreamActivity[];
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  providerUsage?: ProviderUsageSummary;
};

function sourceLabel(event: TraceRecord, rootProcessId: string): string | undefined {
  const processId = String(event.process_id || "");
  return rootProcessId && processId && processId !== rootProcessId ? "Subprocess" : undefined;
}

function callKey(event: TraceRecord): string {
  return `${String(event.process_id || "host")}:${String(event.tool_call_id || event.tool_name || "")}`;
}

/**
 * Convert the authoritative SQLite trace into the same ordered model used by
 * the live main/subprocess renderer. Text followed by another provider/tool step
 * is progress; the final response draft is omitted because TaskOutcome is the
 * one public final answer rendered after the completed capsule.
 */
export function timelineFromTrace(
  events: TraceRecord[],
  rootProcessId = "",
  terminal?: {status?: string; timestamp?: string},
): TurnTimelineModel {
  const activity: StreamActivity[] = [];
  const tools: ToolCallCard[] = [];
  const notices: RuntimeNotice[] = [];
  const toolIndex = new Map<string, number>();
  const pendingText = new Map<string, {text: string; visibility: "public" | "verbose"; actor_label?: string}>();
  let providerUsage: ProviderUsageSummary | undefined;

  const flushProgress = (processId: string) => {
    const pending = pendingText.get(processId);
    pendingText.delete(processId);
    const text = pending?.text.trim();
    if (!pending || !text) return;
    activity.push({
      kind: "progress", text, visibility: "public",
      process_id: processId || undefined, actor_label: pending.actor_label,
    });
  };

  for (const event of events) {
    const processId = String(event.process_id || "host");
    const actor_label = sourceLabel(event, rootProcessId);
    if (event.event === "provider_usage") {
      const usage = event.usage && typeof event.usage === "object"
        ? event.usage as Record<string, unknown> : {};
      providerUsage = {
        input_tokens: (providerUsage?.input_tokens || 0) + Number(usage.input_tokens || 0),
        output_tokens: (providerUsage?.output_tokens || 0) + Number(usage.output_tokens || 0),
      };
      continue;
    }
    if (event.event === "text_delta") {
      const previous = pendingText.get(processId);
      pendingText.set(processId, {
        text: (previous?.text || "") + String(event.delta || ""),
        visibility: event.visibility === "public" ? "public" : previous?.visibility || "verbose",
        actor_label,
      });
      continue;
    }
    if (event.event === "reasoning_delta") {
      const delta = String(event.delta || "");
      const last = activity[activity.length - 1];
      if (last?.kind === "reasoning" && last.process_id === processId) {
        activity[activity.length - 1] = {...last, text: last.text + delta};
      } else if (delta) {
        activity.push({kind: "reasoning", text: delta, process_id: processId, actor_label});
      }
      continue;
    }
    if (event.event === "progress_summary") {
      const message = String(event.message || "").trim();
      if (message) activity.push({
        kind: "progress", text: message, visibility: "public",
        process_id: processId, actor_label,
      });
      continue;
    }
    if (event.event === "provider_request_started" || event.event === "toolcall_start") {
      flushProgress(processId);
    }
    if (event.event === "agent_terminal" || event.event === "agent_complete") {
      // The terminal provider prose is the TaskOutcome rendered below the
      // capsule. Keeping it here would duplicate the final answer.
      pendingText.delete(processId);
    }

    if (event.event === "toolcall_start") {
      const index = tools.length;
      const key = callKey(event);
      toolIndex.set(key, index);
      tools.push({
        tool_name: String(event.tool_name || "tool"),
        tool_call_id: String(event.tool_call_id || ""),
        target: String(event.target || ""), allowed: true,
        status_label: "running", duration_ms: 0,
        timestamp: String(event.time || ""), is_running: true, state: "running",
        actor_label, process_id: String(event.process_id || ""),
        detail_ref: String(event.detail_ref || "") || undefined,
      });
      activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      continue;
    }
    if (event.event === "tool_result") {
      const key = callKey(event);
      let index = toolIndex.get(key);
      if (index === undefined) {
        index = tools.length;
        toolIndex.set(key, index);
        tools.push({
          tool_name: String(event.tool_name || "tool"),
          tool_call_id: String(event.tool_call_id || ""), target: "",
          allowed: true, status_label: "", duration_ms: 0,
          timestamp: String(event.time || ""), actor_label,
          process_id: String(event.process_id || ""),
        });
        activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      }
      const failed = Boolean(event.is_error);
      tools[index] = {
        ...tools[index], allowed: !failed, is_running: false,
        state: failed ? "error" : "completed",
        status_label: failed ? "ERROR" : "OK",
        duration_ms: Number(event.duration_ms || 0),
        result_text: String(event.result_preview || event.error || ""),
        compact_summary: String(event.compact_summary || "") || undefined,
        blocked_reason: failed ? String(event.error || "tool failed") : undefined,
        diff: String(event.diff || "") || undefined,
        detail_ref: String(event.detail_ref || tools[index].detail_ref || "") || undefined,
      };
      continue;
    }
    if (event.event === "composite_step_result") {
      const failed = Boolean(event.is_error);
      const index = tools.length;
      tools.push({
        tool_name: String(event.tool_name || "tool"),
        tool_call_id: `${String(event.execution_id || "")}:${String(event.step_id || "")}`,
        target: `${String(event.composite_tool || "composite")} / ${String(event.step_id || "step")}`,
        allowed: !failed, status_label: failed ? "ERROR" : "OK",
        duration_ms: Number(event.duration_ms || 0), timestamp: String(event.time || ""),
        result_text: String(event.error || "receipt recorded"),
        blocked_reason: failed ? String(event.error || "composite step failed") : undefined,
        is_running: false, state: failed ? "error" : "completed",
        actor_label, process_id: String(event.process_id || ""),
        detail_ref: String(event.detail_ref || "") || undefined,
      });
      activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      continue;
    }

    const projected = runtimeNoticeFromEvent({...event, actor_label} as unknown as StreamEvent);
    if (projected) {
      const index = notices.length;
      notices.push(projected);
      activity.push({kind: "notice", noticeIndex: index, detail_ref: projected.detail_ref});
    }
  }
  if (terminal?.status && !["running", "waiting", "awaiting_user"].includes(terminal.status)) {
    const endedAt = Date.parse(terminal.timestamp || "");
    for (let index = 0; index < tools.length; index += 1) {
      const tool = tools[index]!;
      if (!tool.is_running && tool.state !== "running" && tool.state !== "pending") continue;
      const startedAt = Date.parse(tool.timestamp || "");
      tools[index] = {
        ...tool,
        is_running: false,
        state: "unresolved",
        allowed: false,
        duration_ms: Number.isFinite(endedAt) && Number.isFinite(startedAt)
          ? Math.max(0, endedAt - startedAt) : 0,
        blocked_reason: "No durable tool result was recorded before the turn ended; execution state is unknown.",
      };
    }
  }
  return {activity, tools, notices, providerUsage};
}
