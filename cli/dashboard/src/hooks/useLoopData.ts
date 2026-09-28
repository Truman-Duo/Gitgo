// src/hooks/useLoopData.ts
// Polls gitgo_loop_status every 5s with 16ms event batching (borrowed from OpenCode sdk.tsx)

import { useState, useEffect, useRef, useCallback } from "react";
import type { BackendClient } from "../backend/client.js";
import type { ChatMessage } from "../types.js";
import type { PendingDecision } from "../types.js";
import { usePoll } from "./usePoll.js";
import { loopStatus } from "../backend/tools.js";
import { publishProcessStreamEvent } from "../daemon/processStreams.js";
import type { StreamEvent } from "../daemon/streamEvents.js";

export type ProcessInfo = {
  archived?: boolean;
  display_name?: string;
  session_id?: string;
  process_id: string;
  role: string;
  ring_level: number;
  status: string;
  steps_used: number;
  max_steps: number;
  parent_id: string | null;
  parent_ids?: string[];
  depends_on?: string[];
  downstream_ids?: string[];
  child_ids?: string[];
  child_contracts?: Record<string, Record<string, unknown>>;
  child_reviews?: Record<string, Record<string, unknown>>;
  created_at: string;
  worktree_path: string;
  provider_id: string;
  model_id: string;
  estimated_tokens: number;
  active_task_id?: string;
  task_kind?: string;
  actor_kind?: string;
  capability_profile_id?: string;
  relationship_policy?: Record<string, unknown>;
  coordination?: {
    pending_count?: number;
    blocked_by_event_ids?: string[];
    latest?: Array<Record<string, unknown>>;
  };
  pending_decision?: PendingDecision | null;
  recovery?: Omit<RecoveryCandidate, "process_id" | "status"> | null;
  mailbox?: Record<string, unknown> | null;
  task_budget?: Record<string, unknown> | null;
  context?: {
    estimated_tokens: number;
    used_tokens?: number;
    provider_reported_tokens?: number;
    measurement?: "provider" | "estimated" | string;
    limit: number;
    free_tokens?: number;
    auto_compact_tokens?: number;
    auto_compact_enabled?: boolean;
    tool_count?: number;
    epoch: number;
    breakdown?: {
      estimated_tokens?: number;
      sections?: Array<{name: string; estimated_tokens: number; ratio: number}>;
    };
  };
  cache_summary?: {
    input_tokens?: number;
    cache_read_tokens?: number;
    raw_hit_ratio?: number;
    eligible_input_tokens?: number;
    eligible_cache_read_tokens?: number;
    eligible_hit_ratio?: number;
    miss_reasons?: string[];
  };
  worktree?: {
    path: string;
    isolated: boolean;
    state: "isolated" | "shared_workspace" | string;
    base_commit?: string;
    output_commit?: string;
    snapshot_commit?: string;
    own_commit?: string;
    promoted?: boolean;
    promoted_at?: string;
    dirty?: boolean | null;
  };
};

export type ToolEvent = {
  timestamp: string;
  process_id: string;
  tool_name: string;
  allowed: boolean;
  duration_ms: number;
  role: string;
  blocked_reason?: string;
  diff?: string;
};

export type ProviderHealth = {
  id: string;
  breaker_state: string;   // "closed" | "open" | "half_open"
  failures: number;
  available: boolean;
  health_supported?: boolean;
};

export type StorageHealth = {
  level: "ok" | "warning" | "degraded" | "blocked" | string;
  reasons?: string[];
  state_bytes?: number;
  observability_bytes?: number;
  cas_bytes?: number;
  total_bytes?: number;
  free_bytes?: number;
  message?: string;
};

export type RecoveryCandidate = {
  process_id: string;
  task_id?: string;
  session_id?: string;
  role?: string;
  status: string;
  parent_id?: string | null;
  requires_manual_verification?: boolean;
  resume_forbidden?: boolean;
  reasons?: Array<Record<string, unknown>>;
  self_execute_lease_revoked?: boolean;
  dynamic_tools_revoked?: string[];
};

export type LoopData = {
  processes: Record<string, ProcessInfo>;
  toolEvents: ToolEvent[];
  providers: ProviderHealth[];
  daemonOnline: boolean;
  loading: boolean;
  error: string | null;
  mainConversation: ChatMessage[] | undefined;
  agentConversations: Record<string, ChatMessage[]> | undefined;
  recoveryAvailable: string[];
  recoveryCandidates: RecoveryCandidate[];
  storage: StorageHealth | null;
  pendingQuestions: PendingDecision[];
  refresh: () => Promise<void>;
};

export function useLoopData(
  client: BackendClient | null,
  project: string | null,
  refreshSec: number = 5,
): LoopData {
  const [processes, setProcesses] = useState<Record<string, ProcessInfo>>({});
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [daemonOnline, setDaemonOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mainConversation, setMainConversation] = useState<ChatMessage[] | undefined>(undefined);
  const [agentConversations, setAgentConversations] = useState<Record<string, ChatMessage[]> | undefined>(undefined);
  const [recoveryAvailable, setRecoveryAvailable] = useState<string[]>([]);
  const [recoveryCandidates, setRecoveryCandidates] = useState<RecoveryCandidate[]>([]);
  const [storage, setStorage] = useState<StorageHealth | null>(null);
  const [pendingQuestions, setPendingQuestions] = useState<PendingDecision[]>([]);
  const [loadedProject, setLoadedProject] = useState<string | null>(null);
  const projectRef = useRef(project);
  projectRef.current = project;
  const lastProjectionRef = useRef<{project: string; fingerprint: string} | null>(null);
  const eventRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const initializationRetryRef = useRef(0);

  // 16ms batch window (OpenCode pattern)
  const batchRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<{
    project: string;
    processes: Record<string, ProcessInfo>;
    toolEvents: ToolEvent[];
    providers: ProviderHealth[];
    daemonOnline: boolean;
    mainConversation: ChatMessage[] | undefined;
    agentConversations: Record<string, ChatMessage[]> | undefined;
    recoveryAvailable: string[];
    recoveryCandidates: RecoveryCandidate[];
    storage: StorageHealth | null;
    pendingQuestions: PendingDecision[];
  } | null>(null);

  const flushBatch = useCallback(() => {
    if (!pendingRef.current) return;
    const p = pendingRef.current;
    pendingRef.current = null;
    if (p.project !== projectRef.current) return;
    setProcesses(p.processes);
    setToolEvents(p.toolEvents);
    setProviders(p.providers);
    setDaemonOnline(p.daemonOnline);
    setMainConversation(p.mainConversation);
    setAgentConversations(p.agentConversations);
    setRecoveryAvailable(p.recoveryAvailable);
    setRecoveryCandidates(p.recoveryCandidates);
    setStorage(p.storage);
    setPendingQuestions(p.pendingQuestions);
    setLoadedProject(p.project);
    setError(null);
  }, []);

  const fetchData = useCallback(async () => {
    if (!client || !project) return;
    let projectionStillInitializing = false;
    try {
      const requestedProject = project;
      const result: any = await loopStatus(client, requestedProject);
      if (projectRef.current !== requestedProject) return;
      if (String(result?.storage?.level || "") === "initializing") {
        // A cold Native Host intentionally answers before the SQLite projection
        // is ready. That response is a readiness signal, not an authoritative
        // empty transcript. Keep the previous conversation/loading state and
        // retry promptly even if the one-shot ready event was missed.
        projectionStillInitializing = true;
        setDaemonOnline(result?.daemon_online ?? false);
        setStorage((result?.storage || null) as StorageHealth | null);
        setLoading(true);
        const attempt = initializationRetryRef.current++;
        const delay = Math.min(2000, 100 * (2 ** Math.min(attempt, 5)));
        if (eventRefreshRef.current) clearTimeout(eventRefreshRef.current);
        eventRefreshRef.current = setTimeout(() => {
          eventRefreshRef.current = null;
          void fetchData();
        }, delay);
        return;
      }
      initializationRetryRef.current = 0;
      // Polling is an authoritative reconciliation path, not a repaint clock.
      // Re-applying an identical deep projection every two seconds forces Ink
      // to revisit the full conversation tree and makes command navigation
      // appear to flicker even though no runtime fact changed.
      const fingerprint = JSON.stringify({
        processes: result?.processes || {},
        tools: result?.recent_tool_executed || [],
        providers: result?.providers || [],
        main: result?.main_conversation || [],
        agents: result?.agent_conversations || {},
        recovery: result?.recovery_candidates || [],
        storage: result?.storage || null,
        questions: result?.pending_questions || [],
      });
      if (lastProjectionRef.current?.project === requestedProject
          && lastProjectionRef.current.fingerprint === fingerprint) {
        setDaemonOnline(result?.daemon_online ?? false);
        setLoading(false);
        return;
      }
      lastProjectionRef.current = {project: requestedProject, fingerprint};
      const procs: Record<string, ProcessInfo> = {};
      if (result?.processes) {
        for (const [pid, p] of Object.entries(result.processes)) {
          procs[pid] = p as ProcessInfo;
        }
      }
      // Batch: enqueue and flush on 16ms window
      pendingRef.current = {
        project: requestedProject,
        processes: procs,
        toolEvents: (result?.recent_tool_executed || []) as ToolEvent[],
        providers: (result?.providers || []) as ProviderHealth[],
        daemonOnline: result?.daemon_online ?? false,
        mainConversation: (result?.main_conversation || undefined) as ChatMessage[] | undefined,
        agentConversations: (result?.agent_conversations || undefined) as Record<string, ChatMessage[]> | undefined,
        recoveryAvailable: (result?.recovery_available || []) as string[],
        recoveryCandidates: (result?.recovery_candidates || []) as RecoveryCandidate[],
        storage: (result?.storage || null) as StorageHealth | null,
        pendingQuestions: (result?.pending_questions || []) as PendingDecision[],
      };
      if (batchRef.current) clearTimeout(batchRef.current);
      batchRef.current = setTimeout(flushBatch, 16);
    } catch (e: any) {
      if (projectRef.current === project) setError(e.message);
    } finally {
      if (!projectionStillInitializing) setLoading(false);
    }
  }, [client, project, flushBatch]);

  useEffect(() => {
    if (!client?.subscribeEvents || !project) return;
    const stateEvents = new Set([
      "runtime_ack", "task_admitted", "agent_started", "agent_terminal", "agent_complete",
      "state_changed", "daemon_started", "daemon_stopped",
      "provider_usage", "tool_result", "decision_required", "user_decision_received",
      "decision_resumed", "context_compaction_requested", "context_compaction_completed",
      "context_compaction_failed", "session_rewound", "session_recovery_resumed",
      "session_recovery_discarded", "session_recovery_blocked", "workspace_dirty",
      "governance_signals", "lessons_harvested", "lesson_harvest_failed", "storage_health",
      "runtime_projection_ready", "coordination_event",
      "coordination_event_resolved", "coordination_observation_failed",
    ]);
    const unsubscribe = client.subscribeEvents(event => {
      if (String(event.project || "") !== project) return;
      // Correlated root events are already reduced by sendChat. Delegated B
      // events deliberately have no request id, so project subscribers own
      // their process-scoped live stream. This keeps A and B text isolated
      // while making B reasoning/tool/diff activity visible immediately.
      if (!event.request_id && event.process_id) {
        publishProcessStreamEvent(
          project,
          event as StreamEvent,
          String(event.timestamp || new Date().toISOString()),
        );
      }
      if (!stateEvents.has(String(event.event || ""))) return;
      if (eventRefreshRef.current) clearTimeout(eventRefreshRef.current);
      eventRefreshRef.current = setTimeout(() => {
        eventRefreshRef.current = null;
        void fetchData();
      }, 50);
    });
    return () => {
      unsubscribe();
      if (eventRefreshRef.current) clearTimeout(eventRefreshRef.current);
      eventRefreshRef.current = null;
    };
  }, [client, project, fetchData]);

  // As with the project list, subscribe before issuing the initial snapshot
  // read so early lifecycle events cannot fall into the mount-time gap.
  usePoll(fetchData, Math.max(30, refreshSec) * 1000, [fetchData, refreshSec]);

  useEffect(() => { lastProjectionRef.current = null; }, [project]);

  // Ensure batchRef is cleaned up on unmount
  useEffect(() => {
    return () => {
      if (batchRef.current) clearTimeout(batchRef.current);
      if (eventRefreshRef.current) clearTimeout(eventRefreshRef.current);
      initializationRetryRef.current = 0;
    };
  }, []);

  const current = Boolean(project && loadedProject === project);
  return {
    processes: current ? processes : {},
    toolEvents: current ? toolEvents : [],
    providers: current ? providers : [],
    daemonOnline: current ? daemonOnline : false,
    loading: Boolean(project) && (!current || loading),
    error,
    mainConversation: current ? mainConversation : undefined,
    agentConversations: current ? agentConversations : undefined,
    recoveryAvailable: current ? recoveryAvailable : [],
    recoveryCandidates: current ? recoveryCandidates : [],
    storage: current ? storage : null,
    pendingQuestions: current ? pendingQuestions : [],
    refresh: fetchData,
  };
}
