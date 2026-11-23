import type {StreamActivity, ToolCallCard} from "../types.js";

export function toolState(tool: ToolCallCard) {
  return tool.state ?? (tool.is_running ? "running" : tool.allowed ? "completed" : "error");
}
function family(tool: ToolCallCard) {return ["web_search", "hosted_web_search", "web_fetch"].includes(tool.tool_name.toLowerCase()) ? "web" : "";}
function interval(tool: ToolCallCard) {
  const start = Date.parse(tool.timestamp), duration = Number(tool.duration_ms);
  return Number.isFinite(start) && duration > 0 && Number.isFinite(duration) ? {start, end: start + duration} : null;
}
export type TimelineRow = {id: string; activity: StreamActivity; toolIndices?: number[]};
/** Stable within a turn: public filtering and another process cannot renumber reasoning. */
export function timelineRows(activity: StreamActivity[], tools: ToolCallCard[]): TimelineRow[] {
  const counters = new Map<string, number>(), result: TimelineRow[] = [];
  for (let index = 0; index < activity.length; index++) {
    const item = activity[index]!;
    const tool = item.kind === "tool" ? tools[item.toolIndex] : undefined;
    if(tool?.context_only)continue;
    const process = tool?.process_id ?? ("process_id" in item ? item.process_id : "") ?? "";
    const scope = `${process}:${item.kind}`, ordinal = counters.get(scope) ?? 0; counters.set(scope, ordinal + 1);
    const id = item.record_id || (tool?.tool_call_id ? `tool:${process}:${tool.tool_call_id}`
      : item.kind === "progress" && item.update_id ? `progress:${item.turn_id || ""}:${process}:${item.update_id}`
      : item.trace_refs?.length ? `${scope}:seq:${item.trace_refs[0]!.first_seq}` : `${scope}:${ordinal}`);
    const row: TimelineRow = {id, activity: item};
    if (tool && item.kind === "tool") {
      row.toolIndices = [item.toolIndex];
      let overlap = interval(tool);
      // Adjacency is not evidence of parallel execution. Only observed,
      // completed successes from the same actor may share a summary.
      if (family(tool) && toolState(tool) === "completed" && tool.allowed !== false && overlap) {
        while (index + 1 < activity.length) {
          const next = activity[index + 1]!;
          if (next.kind !== "tool") break;
          const sibling = tools[next.toolIndex], range = sibling && interval(sibling);
          if (!sibling || family(sibling) !== family(tool) || sibling.process_id !== tool.process_id
            || sibling.actor_label !== tool.actor_label || toolState(sibling) !== "completed" || sibling.allowed === false
            || !range || Math.max(overlap.start, range.start) >= Math.min(overlap.end, range.end)) break;
          overlap = {start: Math.max(overlap.start, range.start), end: Math.min(overlap.end, range.end)};
          row.toolIndices.push(next.toolIndex); index++;
        }
      }
    }
    result.push(row);
  }
  return result;
}
