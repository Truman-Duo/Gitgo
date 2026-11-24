import type { ChatMessage } from "../types.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { runtimeNoticeFromEvent } from "../daemon/runtimeNotices.js";
import type { TraceRecord } from "../traceView.js";
import { formatDuration } from "../theme/typography.js";
import type {Copy} from "../ui/pageContract.js";

/** Saved failures expose the same trace without hiding or relabeling their error. */
export function roundLabel(message: ChatMessage, t: (copy: Copy) => string = copy => copy.en): string | null {
  if (message.role !== "assistant") return null;
  if (message.kind === "error" || ["failed", "degraded", "cancelled", "timed_out"].includes(message.status || "")) {
    return t({en:"Failed",zh:"失败"});
  }
  if (message.status === "awaiting_user") return t({en:"Awaiting input",zh:"等待回复"});
  if (message.status === "completed") return t({en:"Completed",zh:"已完成"});
  return t({en:"Process",zh:"过程记录"});
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
