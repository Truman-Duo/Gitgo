import type { ProcessInfo } from "../hooks/useLoopData.js";
/** Human label is presentation only; routing always uses full backend identity. */
export function agentLabel(process: ProcessInfo): string {
  return process.display_name || (process.actor_kind === "supervisor"
    ? "Main process" : process.role || "Subprocess");
}
export function runningWorkers(processes: Record<string, ProcessInfo>): ProcessInfo[] {
  return Object.values(processes).filter(p => Boolean(p.parent_id)
    && p.actor_kind !== "supervisor" && ["running", "waiting", "awaiting_user"].includes(p.status))
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
}
