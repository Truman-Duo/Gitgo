import { truncate, wrap } from "./theme/index.js";

export type TraceRecord = {
  seq: number;
  time: string;
  event: string;
  process_id?: string;
  parent_process_id?: string;
  step?: number;
  delta?: string;
  tool_name?: string;
  accepted?: boolean;
  status?: string;
  detail_ref?: string;
  [key: string]: unknown;
};

export function shortPid(value: unknown): string {
  return String(value || "--------").slice(0, 8);
}

function oneLine(value: unknown, max = 100): string {
  return truncate(String(value ?? "").replace(/\s+/g, " ").trim(), max);
}

export function eventSummary(event: TraceRecord): string {
  switch (event.event) {
    case "reasoning_delta": return `THINK ${oneLine(event.delta)}`;
    case "text_delta": return `TEXT  ${oneLine(event.delta)}`;
    case "toolcall_start": return `TOOL  ${event.tool_name || "?"} started`;
    case "toolcall_done": return `TOOL  ${event.tool_name || "?"} arguments complete`;
    case "tool_result": return `TOOL  ${event.tool_name || "?"} ${event.is_error ? "failed" : "completed"}`;
    case "composite_step_result": return `TOOL  ${event.composite_tool || "?"}/${event.step_id || "?"} → ${event.tool_name || "?"} ${event.is_error ? "failed" : "completed"}`;
    case "provider_request_started": return `LLM   request step ${event.step ?? "?"}`;
    case "provider_response_completed": return `LLM   response ${event.duration_ms ?? "?"} ms`;
    case "provider_response_incomplete": return `LLM   incomplete (${event.reason || "unknown"})`;
    case "provider_usage": return "USAGE provider token/cache sample";
    case "governance_snapshot": return `GOV   version ${event.governance_version ?? "?"} (${event.signal_count ?? 0} signals)`;
    case "completion_gate": return `GATE  ${event.accepted ? "accepted" : "rejected"}`;
    case "context_window_action": return `CTX   ${event.action || "window action"}`;
    case "context_compaction_completed": return `CTX   epoch ${event.context_epoch ?? "?"}`;
    case "agent_started": return `AGENT started ${event.actor_kind || "worker"}`;
    case "agent_terminal": return `AGENT terminal ${event.status || ""}`;
    case "agent_complete": return "AGENT root task complete";
    case "task_bundle_delegated": return `BUNDLE ${event.shard_count ?? "?"} shards delegated`;
    default: return `${event.event} ${oneLine(event.status || "")}`;
  }
}

function payload(event: TraceRecord): string {
  switch (event.event) {
    case "reasoning_delta":
    case "text_delta":
    case "toolcall_delta":
      return String(event.delta || "");
    case "tool_result":
      return String(event.result_preview || event.error || eventSummary(event));
    case "composite_step_result":
      return JSON.stringify({
        composite_tool: event.composite_tool,
        step_id: event.step_id,
        tool_name: event.tool_name,
        is_error: event.is_error,
        error: event.error,
        duration_ms: event.duration_ms,
        receipt: event.receipt,
      }, null, 2);
    case "provider_request_started":
      return `Prompt snapshot ${event.detail_ref || "not stored"}; step=${event.step ?? "?"}`;
    case "provider_usage":
      return JSON.stringify({ usage: event.usage, cache: event.cache }, null, 2);
    case "governance_snapshot":
      return JSON.stringify({
        governance_version: event.governance_version,
        signal_count: event.signal_count,
        detail_ref: event.detail_ref,
      }, null, 2);
    default:
      return eventSummary(event);
  }
}

export function verboseEventLines(event: TraceRecord, width: number): string[] {
  const label = event.event === "reasoning_delta"
    ? "THINK"
    : event.event === "text_delta"
      ? "TEXT"
      : event.event.startsWith("tool")
        ? "TOOL"
        : event.event.startsWith("provider")
          ? "LLM"
          : event.event === "governance_snapshot"
            ? "GOV"
            : "FLOW";
  const time = String(event.time || "").slice(11, 23);
  const header = `${String(event.seq).padStart(5)} ${time} ${shortPid(event.process_id)} ${label} ${event.event}`;
  const body = wrap(payload(event), Math.max(20, width - 4));
  return [header, ...body.map(line => `  ${line}`)];
}

export function mergeTraceEvents(
  current: TraceRecord[], incoming: TraceRecord[], limit = 5000,
): TraceRecord[] {
  const seen = new Set(current.map(item => item.seq));
  return [
    ...current,
    ...incoming.filter(item => !seen.has(item.seq)),
  ].sort((a, b) => a.seq - b.seq).slice(-Math.max(1, limit));
}
