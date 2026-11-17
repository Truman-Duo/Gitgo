// src/hooks/useGitgoData.ts
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { BackendClient } from "../backend/client.js";
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
  countScope?: string;
  activeProcessCount: number;
  waitingProcessCount: number;
  finishedProcessCount?: number;
  durableStatus?: string;
  governanceStatus: string;
  llmProviderSummary: string;
  llmStatus: StatusState;
  lessonsSeverity: string;
};

type Summary = Record<string, any>;
type Observation = {summary: Summary; eventSequence: number; projectId?: string};
// Project names are user data, including names such as "constructor" or "__proto__".
function projectIndex<T>(): Record<string, T> {return Object.create(null);}
function observationTime(summary: Summary): number | null {
  const raw = summary.observed_at;
  if (raw === undefined || raw === null || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}
function mergeObservation(previous: Observation | undefined, incoming: Observation, requestEvents?: number): Observation {
  if (!previous) return incoming;
  if (previous.projectId && incoming.projectId && previous.projectId !== incoming.projectId) return incoming;
  const oldTime = observationTime(previous.summary), newTime = observationTime(incoming.summary);
  if (oldTime !== null && newTime !== null && oldTime !== newTime) return oldTime > newTime ? previous : incoming;
  // An unavailable Host observation can retain the last healthy timestamp.
  // Equal times therefore do not prove that a returned snapshot is newer than
  // an event received during its request. As with legacy untimed summaries,
  // a later request may reconcile the event instead of pinning it forever.
  return requestEvents !== undefined && previous.eventSequence > requestEvents ? previous : incoming;
}

export function useGitgoData(
  client: BackendClient | null,
  refreshSec: number = 5
) {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const lastRowsRef = useRef("");
  const summariesRef = useRef<Record<string, Observation>>(projectIndex());
  const identitiesRef = useRef<Record<string, string>>(projectIndex());
  const projectIdsRef = useRef<Record<string, string>>(projectIndex());
  const eventSequenceRef = useRef(0);
  const eventTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ownerRef = useRef({client, active: false});
  const fetchInFlightRef = useRef<{owner: typeof ownerRef.current; queued: boolean} | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useLayoutEffect(() => {
    const owner = {client, active: true};
    ownerRef.current = owner;
    summariesRef.current = projectIndex();
    identitiesRef.current = projectIndex();
    projectIdsRef.current = projectIndex();
    lastRowsRef.current = "";
    setProjects([]); setError(null); setLoading(Boolean(client));
    return () => {owner.active = false;};
  }, [client]);

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
      countScope: String(summary.count_scope || ""),
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

  // Serialize within a connection generation. A replacement client can load
  // immediately without waiting for an old Host request to settle.
  const refresh = useCallback(async () => {
    const owner = ownerRef.current;
    const current = () => owner.active && ownerRef.current === owner && owner.client === client;
    if (!client || !current()) return;
    if (fetchInFlightRef.current?.owner === owner) {
      fetchInFlightRef.current.queued = true;
      return;
    }
    const flight = {owner, queued: false};
    fetchInFlightRef.current = flight;
    try {
      do {
        flight.queued = false;
        const requestEvents = eventSequenceRef.current;
        try {
          const overview: any = await projectOverview(client);
          if (!current()) return;
          const projectList: Array<ProjectInfo & {runtime_summary?: Summary}> =
            Array.isArray(overview?.projects) ? overview.projects : [];
          const observations = projectIndex<Observation>();
          const identities = projectIndex<string>();
          const projectIds = projectIndex<string>();
          const rows = projectList.map(p => {
            const identity = p.project_id || p.workspace;
            const replaced = identitiesRef.current[p.name] !== undefined && identitiesRef.current[p.name] !== identity;
            const observed = mergeObservation(replaced ? undefined : summariesRef.current[p.name],
              {summary: p.runtime_summary || {}, eventSequence: 0, projectId: p.project_id}, requestEvents);
            observations[p.name] = observed; identities[p.name] = identity;
            if (p.project_id) projectIds[p.name] = p.project_id;
            return projectRow(p, observed.summary);
          });
          summariesRef.current = observations;
          identitiesRef.current = identities;
          projectIdsRef.current = projectIds;
          const flat = sortedRows(rows), fingerprint = JSON.stringify(flat);
          if (fingerprint !== lastRowsRef.current) {
            lastRowsRef.current = fingerprint;
            setProjects(flat);
          }
          setError(null);
        } catch (e: any) {
          if (current()) setError(e instanceof Error ? e.message : String(e));
        }
      } while (current() && flight.queued);
    } finally {
      if (fetchInFlightRef.current === flight) fetchInFlightRef.current = null;
      if (current()) setLoading(false);
    }
  }, [client, projectRow, sortedRows]);

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
      "deletion_status", "storage_health", "project_lifecycle_changed",
    ]);
    const owner = ownerRef.current;
    const unsubscribe = client.subscribeEvents((event) => {
      if (!owner.active || ownerRef.current !== owner) return;
      if (!stateEvents.has(String(event.event || ""))) return;
      if (event.event === "project_summary_updated" && event.project && event.summary) {
        const name = String(event.project);
        const knownProjectId = projectIdsRef.current[name];
        if (event.project_id && knownProjectId && event.project_id !== knownProjectId) return;
        const incoming = {summary: event.summary as Summary, eventSequence: ++eventSequenceRef.current,
          projectId: event.project_id ? String(event.project_id) : knownProjectId};
        const observation = mergeObservation(summariesRef.current[name], incoming);
        if (observation !== incoming) return;
        summariesRef.current[name] = observation;
        const summary = observation.summary;
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
