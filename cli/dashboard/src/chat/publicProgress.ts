import type {StreamActivity} from "../types.js";
import type {TraceBodyRef} from "./traceSource.js";

/** The legacy provider phase was a runtime bootstrap template, not Agent prose.
 * Classify its original protocol metadata; never remove messages by wording. */
export function isRuntimePreparation(event: {event:string;phase?:unknown;update_id?:unknown}): boolean {
  return event.event === "progress_summary" && event.phase === "provider" && !event.update_id;
}

export function progressIdentity(item: {process_id?: string; update_id?: string; task_id?: unknown; turn_id?: string}): string {
  return JSON.stringify([typeof item.task_id === "string" ? item.task_id : item.turn_id || "",
    item.process_id || "host", item.update_id || ""]);
}

/** Shared live/trace projection. The terminal summary replaces its own deltas. */
export function updatePublicProgress(activity: StreamActivity[], event: {
  event: string; process_id?: string; update_id?: string; actor_label?: string;
  delta?: unknown; message?: unknown; progress_status?: unknown;
  task_id?: unknown;
  phase?: unknown;
  trace_refs?: TraceBodyRef[];
  stage_id?:string;
}, indices?: Map<string, number>, mutable = false): StreamActivity[] {
  if(isRuntimePreparation(event))return activity;
  const delta = event.event === "progress_delta";
  const text = String((delta ? event.delta : event.message) || "");
  if (!text && !event.trace_refs?.length) return activity;
  const identity = progressIdentity(event);
  const index = event.update_id ? indices
    ? indices.get(identity) ?? -1
    : activity.findIndex(item => item.kind === "progress" && item.update_id
      && progressIdentity(item) === identity) : -1;
  const previous = index >= 0 ? activity[index] : undefined;
  if (previous?.kind === "progress") {
    const result = mutable ? activity : activity.slice();
    result[index] = {...previous, text: delta ? previous.text + text : text,
      stage_id:event.stage_id ?? previous.stage_id,
      trace_refs: delta ? [...(previous.trace_refs || []), ...(event.trace_refs || [])] : event.trace_refs,
      progress_status: String(event.progress_status || (delta ? "streaming" : "completed"))};
    return result;
  }
  const last = activity.at(-1);
  if (!event.update_id && last?.kind === "progress" && last.text === text
      && last.process_id === event.process_id && last.stage_id === event.stage_id) return activity;
  const item: StreamActivity = {kind: "progress", text, visibility: "public",
    ...(event.stage_id?{stage_id:event.stage_id}:{}),
    ...(event.trace_refs?.length ? {trace_refs: event.trace_refs} : {}),
    process_id: event.process_id, actor_label: event.actor_label,
    ...(event.update_id ? {update_id:event.update_id} : {}),
    ...(typeof event.task_id === "string" ? {turn_id:event.task_id} : {}),
    ...(event.progress_status || event.update_id ? {
      progress_status:String(event.progress_status || (delta ? "streaming" : "completed")),
    } : {})};
  if (event.update_id) indices?.set(identity, activity.length);
  if (mutable) {activity.push(item); return activity;}
  return [...activity, item];
}

