// src/hooks/useGitgoData.ts
import { useCallback, useEffect, useRef, useState } from "react";
import type { BackendClient } from "../backend/client.js";
import { useAsyncPoll } from "./useAsyncPoll.js";
import { projectOverview } from "../backend/tools.js";
import type { StatusState } from "../theme/index.js";
import { projectRuntimeRank } from "../projectRuntimeState.js";

export type ProjectInfo = {
  name: string;
  workspace: string;
  project_id?: string;
  backup: string;
  commit_prefix: string;
};

export type ProjectRow = ProjectInfo & {
  pendingLessons: number;
  features: number;
  constraints: number;
  techStack: string;
  daemonOnline: boolean;
  stateAvailable: boolean;
  stateError?: string;
  activeProcessCount: number;
  waitingProcessCount: number;
  finishedProcessCount?: number;
  durableStatus?: string;
  governanceStatus: string;
  llmProviderSummary: string;
  llmStatus: StatusState;
  lessonsSeverity: string;
};

export function useGitgoData(
  client: BackendClient | null,
  refreshSec: number = 5
) {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const lastRowsRef = useRef("");
  const pendingSummariesRef = useRef<Record<string, Record<string, any>>>({});
  const eventTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchInFlightRef = useRef(false);
  const fetchQueuedRef = useRef(false);
  const { loading, error, run } = useAsyncPoll(true);

  const projectRow = useCallback((
    p: ProjectInfo & {runtime_summary?: Record<string, any>},
    override?: Record<string, any>,
  ): ProjectRow => {
    const summary = override || p.runtime_summary || {};
    const { runtime_summary: _runtimeSummary, ...project } = p;
    return {
      ...project, pendingLessons: 0, features: 0, constraints: 0, techStack: "-",
      daemonOnline: Boolean(summary.daemon_online),
      stateAvailable: summary.state_available !== false,
      stateError: String(summary.summary_error || ""),
      activeProcessCount: Number(summary.active_process_count || 0),
      waitingProcessCount: Number(summary.waiting_process_count || 0),
      finishedProcessCount: Number(summary.finished_process_count || 0),
      durableStatus: String(summary.durable_status || ""),
      governanceStatus: "Unknown", llmProviderSummary: "",
      llmStatus: "offline" as StatusState, lessonsSeverity: "none",
    };
  }, []);

  const sortedRows = useCallback((rows: ProjectRow[]) => [...rows].sort((left, right) =>
    projectRuntimeRank(left) - projectRuntimeRank(right) || left.name.localeCompare(right.name)), []);

  const fetchData = useCallback(async () => {
    if (!client) return;
    await run(async () => {
      const overview: any = await projectOverview(client);
      const projectList: Array<ProjectInfo & {runtime_summary?: Record<string, any>}> =
        Array.isArray(overview?.projects) ? overview.projects : [];
      const rows: ProjectRow[] = projectList.map((p) => projectRow(
        p, pendingSummariesRef.current[p.name],
      ));

      // Group: running → pending → unavailable → finished → new.
      const flat = sortedRows(rows);

      // Atomic and change-aware: a project-list poll must not repaint an open
      // chat or its command suggestions when the overview is byte-identical.
      const fingerprint = JSON.stringify(flat);
      if (fingerprint !== lastRowsRef.current) {
        lastRowsRef.current = fingerprint;
        setProjects(flat);
      }
    }, { loading: false });
  }, [client, run, projectRow, sortedRows]);

  // One owner for every overview request, including mount, semantic-event
  // reconciliation and manual refresh. This avoids React development remounts
  // or bursts of lifecycle events creating competing native calls.
  const refresh = useCallback(async () => {
    if (fetchInFlightRef.current) {
      fetchQueuedRef.current = true;
      return;
    }
    fetchInFlightRef.current = true;
    try {
      do {
        fetchQueuedRef.current = false;
        await fetchData();
      } while (fetchQueuedRef.current);
    } finally {
      fetchInFlightRef.current = false;
    }
  }, [fetchData]);

  // Project state is driven by user/agent lifecycle events. A slow background
  // reconciliation remains as crash/lost-event protection, not as the normal
  // refresh path.
  useEffect(() => {
    if (!client?.subscribeEvents) return;
    const stateEvents = new Set([
      "runtime_ack", "task_admitted", "agent_started", "agent_complete", "agent_terminal",
      "state_changed", "daemon_started", "daemon_stopped",
      "project_summary_updated",
      "decision_required", "user_decision_received", "decision_resumed",
      "session_recovery_resumed", "session_recovery_discarded",
      "session_recovery_blocked", "session_rewound",
      "context_compaction_completed", "context_compaction_failed",
      "deletion_status", "storage_health",
    ]);
    const unsubscribe = client.subscribeEvents((event) => {
      if (!stateEvents.has(String(event.event || ""))) return;
      if (event.event === "project_summary_updated" && event.project && event.summary) {
        const name = String(event.project);
        const summary = event.summary as Record<string, any>;
        pendingSummariesRef.current[name] = summary;
        setProjects(current => {
          if (!current.some(row => row.name === name)) return current;
          const next = sortedRows(current.map(row => row.name === name
            ? projectRow(row, summary) : row));
          lastRowsRef.current = JSON.stringify(next);
          return next;
        });
        return;
      }
      if (eventTimerRef.current) clearTimeout(eventTimerRef.current);
      eventTimerRef.current = setTimeout(() => {
        eventTimerRef.current = null;
        void refresh();
      }, 80);
    });
    return () => {
      unsubscribe();
      if (eventTimerRef.current) clearTimeout(eventTimerRef.current);
      eventTimerRef.current = null;
    };
  }, [client, refresh, projectRow, sortedRows]);

  // Registering this effect after the subscriber is intentional: cold summary
  // completions are observable before the first overview starts. The timer is
  // only lost-event recovery; ordinary state changes arrive as events.
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), Math.max(60, refreshSec) * 1000);
    return () => clearInterval(timer);
  }, [refresh, refreshSec]);

  return { projects, loading, error, refresh };
}
