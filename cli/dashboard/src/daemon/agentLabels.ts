import type { ProcessInfo } from "../hooks/useLoopData.js";
import type { Copy } from "../ui/pageContract.js";

type ExecutionIdentity = {process_id?: string; role?: unknown; temporary_subagent?: unknown};

export function isTemporarySubagent(event: ExecutionIdentity): boolean {
  return event.role === "subagent" || event.temporary_subagent === true;
}

export function childActorLabel(event: ExecutionIdentity, rootProcessId: string): string | undefined {
  if (isTemporarySubagent(event)) return "Subagent";
  if (rootProcessId && event.process_id && event.process_id !== rootProcessId) return "Subprocess";
  return undefined;
}

export function actorLabelCopy(label: string): Copy {
  if (label === "Subagent") return {en:label,zh:"子代理"};
  if (label === "Subprocess") return {en:label,zh:"子进程"};
  return {en:label,zh:label};
}

export function promotionSource(event: {promotion?: unknown; promotion_source?: unknown},
  knownSubagents?: ReadonlySet<string>,
): "subagent" | "subprocess" | "mixed" | "unknown" {
  const source = event.promotion_source;
  if (source === "subagent" || source === "subprocess" || source === "mixed") return source;
  // Older captures carry the helper identity on each child event. A promotion
  // event itself belongs to the parent, so use its actual contributing IDs.
  const promotion = event.promotion as {process_ids?: unknown} | undefined;
  const ids = promotion?.process_ids;
  if (knownSubagents && Array.isArray(ids)) {
    const helpers = ids.filter(id => typeof id === "string" && knownSubagents.has(id)).length;
    if (helpers > 0) return helpers === ids.length ? "subagent" : "mixed";
    if (ids.length) return "subprocess";
  }
  return "unknown";
}

/** Human label is presentation only; routing always uses full backend identity. */
export function agentLabel(process: ProcessInfo): string {
  return process.display_name || (process.actor_kind === "supervisor"
    ? "Main process" : process.role || "Subprocess");
}
export function runningWorkers(processes: Record<string, ProcessInfo>): ProcessInfo[] {
  return Object.values(processes).filter(p => Boolean(p.parent_id)
    && p.actor_kind !== "supervisor" && (["running", "waiting", "awaiting_user"].includes(p.status) || ["pending", "failed"].includes(p.closeout_state || "")))
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
}
