import type { ChatMessage } from "../types.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { runtimeNoticeFromEvent } from "../daemon/runtimeNotices.js";
import type { TraceRecord } from "../traceView.js";
import { formatDuration } from "../theme/typography.js";

/** A capsule describes a successful/paused round, never masks an error report. */
export function roundLabel(message: ChatMessage): string | null {
  if (message.role !== "assistant" || message.kind === "error") return null;
  if (message.status === "awaiting_user") return "Awaiting input";
  if (message.status === "completed") return "Completed";
  return null;
}

export function roundTraceEvents(events: TraceRecord[], processId?: string): TraceRecord[] {
  return events.filter(event => !processId || event.process_id === processId);
}

/** Compact and full mode consume the same persisted events as the live UI. */
export function compactEventLine(event: TraceRecord): string | null {
  if (event.event === "tool_result" || event.event === "composite_step_result") {
    return `${event.is_error ? "Tool failed" : "Used"} · ${event.tool_name || "tool"}`
      + ` · ${formatDuration(Number(event.duration_ms || 0))}`
      + (event.is_error && event.error ? ` · ${event.error}` : "");
  }
  if (event.event === "provider_response_completed") {
    return `Model response received · ${formatDuration(Number(event.duration_ms || 0))}`;
  }
  const notice = runtimeNoticeFromEvent(event as StreamEvent);
  return notice ? notice.label + (notice.detail ? ` · ${notice.detail}` : "") : null;
}
