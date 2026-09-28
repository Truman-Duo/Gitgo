import type { RuntimeNotice, RuntimeNoticeSeverity } from "../types.js";
import type { StreamEvent, TraceLifecycleEvent } from "./streamEvents.js";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function percent(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : "?";
}

function short(value: unknown, maximum = 120): string {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > maximum ? `${text.slice(0, Math.max(0, maximum - 1))}…` : text;
}

function noticeKey(event: TraceLifecycleEvent, label: string, detail: string): string {
  const seq = event.seq;
  if (typeof seq === "number" || typeof seq === "string") return `trace:${seq}`;
  const identity = event.decision_id || event.detail_ref || event.dossier_ref ||
    event.context_epoch || event.governance_version || event.step || event.status || detail;
  return [event.event, event.process_id || "host", identity || label].join(":");
}

function makeNotice(
  event: TraceLifecycleEvent,
  label: string,
  detail = "",
  severity: RuntimeNoticeSeverity = "info",
): RuntimeNotice {
  return {
    key: noticeKey(event, label, detail),
    kind: event.event,
    label,
    detail: detail || undefined,
    severity,
    timestamp: typeof event.time === "string" ? event.time : undefined,
    process_id: event.process_id,
    detail_ref: typeof event.detail_ref === "string" ? event.detail_ref : undefined,
    actor_label: typeof event.actor_label === "string" ? event.actor_label : undefined,
  };
}

/** Project a verbose Host event into one compact, user-auditable trajectory row. */
export function runtimeNoticeFromEvent(event: StreamEvent): RuntimeNotice | null {
  if (!("event" in event)) return null;
  const ev = event as TraceLifecycleEvent;
  switch (ev.event) {
    case "stream_recovery":
      return makeNotice(
        ev, "Stream recovery",
        `attempt ${ev.attempt ?? "?"}/${ev.max ?? "?"}`, "warning",
      );
    // Routine control-plane churn remains available in the trace/stats view.
    // It is not user work and must not crowd the conversational trajectory.
    case "task_admitted": return null;
    case "mailbox_applied":
      return makeNotice(ev, "Feedback applied", short(ev.summary || ev.kind || "safe boundary"), "success");
    case "deadline_extended": return null;
    case "provider_request_started": return null;
    case "provider_response_completed": return null;
    case "provider_response_incomplete":
      return makeNotice(ev, "Provider response incomplete", short(ev.reason || "unknown"), "warning");
    case "provider_capability_fallback":
      return makeNotice(
        ev,
        "Provider capability fallback",
        `${short(ev.capability || "hosted_web_search")} · ${short(ev.from || "provider")} → ${short(ev.to || "fallback")} · HTTP ${ev.status_code ?? "?"}`,
        "warning",
      );
    // Usage belongs to the first Thinking row. Cache is represented by the
    // footer status dot, so neither becomes a second timeline event.
    case "provider_usage": return null;
    case "governance_snapshot": return null;
    case "context_window_action":
      return makeNotice(
        ev,
        "Context window",
        `${ev.action || "updated"} · ${percent(ev.usage_ratio)} · epoch ${ev.context_epoch ?? "?"}`,
        ev.action === "hard_truncate" ? "warning" : "info",
      );
    case "context_compaction_completed":
      return makeNotice(
        ev,
        "Context compacted",
        `epoch ${ev.previous_epoch ?? "?"} → ${ev.context_epoch ?? "?"}${ev.changed === false ? " · unchanged" : ""}`,
        ev.changed === false ? "warning" : "success",
      );
    case "context_compaction_requested":
      return makeNotice(ev, "Context compaction started", short(ev.trigger || "manual"));
    case "context_compaction_failed":
      return makeNotice(
        ev, "Context compaction failed",
        short(asRecord(ev.error).message || ev.reason || "retry available"), "error",
      );
    case "lessons_harvested":
      if (Number(ev.count || 0) <= 0) {
        return makeNotice(
          ev, "Knowledge review complete", "No new lesson added", "success",
        );
      }
      return makeNotice(
        ev,
        "Knowledge harvested",
        `${Number(ev.count || 0)} pending lesson${Number(ev.count || 0) === 1 ? "" : "s"} · review in /runtime lesson`,
        "success",
      );
    case "lesson_harvest_failed":
      return makeNotice(
        ev,
        "Knowledge harvest deferred",
        short(asRecord(ev.error).message || ev.error || ev.reason || "retry available"),
        "warning",
      );
    case "lessons_verified":
      return makeNotice(
        ev, "Knowledge verified", `${Number(ev.count || 0)} lesson${Number(ev.count || 0) === 1 ? "" : "s"}`, "success",
      );
    case "lessons_discarded":
      return makeNotice(
        ev, "Knowledge cleaned", `${Number(ev.count || 0)} invalid pending lesson${Number(ev.count || 0) === 1 ? "" : "s"}`, "info",
      );
    case "provider_switched":
      return makeNotice(ev, "Provider switched", "Runtime will reload at the next safe turn", "success");
    case "completion_gate":
      return ev.accepted ? null : makeNotice(
        ev, "Completion blocked", short(ev.reason || ev.source || ""), "warning",
      );
    case "agent_started":
      return makeNotice(ev, "Subprocess started", `${short(ev.actor_kind || "worker")} · ${short(ev.capability_profile_id || "")}`);
    case "agent_terminal":
      return makeNotice(
        ev,
        "Subprocess finished",
        short(ev.status || "unknown"),
        ev.status === "completed" ? "success" : "warning",
      );
    case "agent_complete":
      return makeNotice(ev, "Main process complete", short(ev.status || ""), "success");
    case "task_bundle_delegated":
      return makeNotice(ev, "Task bundle delegated", `${ev.shard_count ?? "?"} shards · ${short(ev.dossier_ref || "")}`);
    case "agent_dag_admitted": {
      const nodes = asRecord(ev.nodes);
      const order = Array.isArray(ev.order) ? ev.order.map((item) => short(item, 24)).join(" → ") : "";
      return makeNotice(ev, "Process DAG admitted", `${Object.keys(nodes).length} nodes${order ? ` · ${order}` : ""}`);
    }
    case "multi_agent_routed_to_supervisor":
      return makeNotice(ev, "Process request routed", "Main-process control plane");
    case "coordination_event": {
      const affected = Array.isArray(ev.affected_process_ids)
        ? ev.affected_process_ids.length : 0;
      return makeNotice(
        ev,
        ev.kind === "interface_revision_proposed"
          ? "Interface revision pending" : "Process coordination update",
        `${short(ev.summary || ev.kind || "")}${affected ? ` · ${affected} affected` : ""}`,
        ev.requires_supervisor_action ? "warning" : "info",
      );
    }
    case "coordination_event_resolved":
      return makeNotice(
        ev, "Process coordination resolved",
        `${short(ev.status || "resolved")} · ${short(asRecord(ev.resolution).note || ev.summary || "")}`,
        ev.status === "accepted" || ev.status === "resolved" ? "success" : "warning",
      );
    case "coordination_observation_failed":
      return makeNotice(ev, "Interface observation failed", short(ev.error || ""), "error");
    case "worktree_leased": {
      const worktree = asRecord(ev.worktree);
      return makeNotice(ev, "Worktree leased", short(worktree.path || ev.process_id || ""));
    }
    case "worktree_sealed": {
      const worktree = asRecord(ev.worktree);
      return makeNotice(ev, "Worktree sealed", short(worktree.result_commit || worktree.own_commit || ev.process_id || ""), "success");
    }
    case "worktree_promoted":
      return makeNotice(
        ev,
        "Subprocess changes promoted",
        Array.isArray(asRecord(ev.promotion).process_ids)
          ? `${(asRecord(ev.promotion).process_ids as unknown[]).length} DAG nodes`
          : short(ev.process_id || ""),
        "success",
      );
    case "worktree_cleanup_failed":
      return makeNotice(ev, "Worktree cleanup failed", short(ev.reason || ev.process_id || ""), "error");
    case "decision_required": {
      const decision = asRecord(ev.decision);
      const options = Array.isArray(decision.options) ? decision.options.length : 0;
      const enriched = { ...ev, decision_id: decision.decision_id } as TraceLifecycleEvent;
      return makeNotice(enriched, "User decision required", `${options} options · ${short(decision.question || "")}`, "warning");
    }
    case "user_decision_received":
      return makeNotice(
        ev, "User decision received",
        short(ev.selected_label || ev.answer || ev.selected_action || "response recorded"), "success",
      );
    case "decision_resumed":
      return makeNotice(ev, "Process resumed after decision", short(ev.decision_id || ""), "success");
    case "sessions_recovery_available": {
      const sessions = Array.isArray(ev.sessions) ? ev.sessions.length : Number(ev.count || 0);
      return makeNotice(ev, "Recovery available", `${sessions} incomplete sessions`, "warning");
    }
    case "session_recovery_resumed":
      return makeNotice(ev, "Recovery resumed", short(ev.process_id || ""), "success");
    case "session_recovery_discarded":
      return makeNotice(ev, "Recovery discarded", short(ev.reason || ev.process_id || ""), "warning");
    case "session_recovery_blocked":
      return makeNotice(ev, "Recovery blocked", short(ev.reason || "manual verification required"), "error");
    case "storage_health": {
      const storage = asRecord(ev.storage);
      const level = String(storage.level || "unknown");
      const reasons = Array.isArray(storage.reasons)
        ? storage.reasons.map((reason) => short(reason, 60)).join(", ")
        : short(storage.message || "");
      return makeNotice(
        ev,
        `Storage ${level}`,
        reasons,
        level === "blocked" ? "error" : level === "ok" ? "success" : "warning",
      );
    }
    case "repository_scope_blocked":
      return makeNotice(
        ev,
        "Repository scope blocked",
        Array.isArray(ev.reasons) ? ev.reasons.map((reason) => short(reason, 60)).join(", ") : "unsafe root",
        "error",
      );
    case "repository_scope_warning":
      return makeNotice(ev, "Repository scope warning", short(ev.reason || "scope could not be verified"), "warning");
    case "toolcall_done":
      return null; // The tool card is the canonical UI for tool-call lifecycle.
    default:
      return null;
  }
}
