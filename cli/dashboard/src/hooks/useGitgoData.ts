// src/hooks/useGitgoData.ts
import { useState } from "react";
import { McpClient } from "../mcp/client.js";
import { usePoll } from "./usePoll.js";
import { useAsyncPoll } from "./useAsyncPoll.js";
import { listProjects, lessonList, contractShow, loopStatus } from "../mcp/tools.js";
import { partitionByRank } from "../theme/index.js";
import type { StatusState } from "../theme/index.js";

export type ProjectInfo = {
  name: string;
  workspace: string;
  backup: string;
  commit_prefix: string;
};

export type ProjectRow = ProjectInfo & {
  pendingLessons: number;
  features: number;
  constraints: number;
  techStack: string;
  daemonOnline: boolean;
  activeProcessCount: number;
  waitingProcessCount: number;
  governanceStatus: string;
  llmProviderSummary: string;
  llmStatus: StatusState;
  lessonsSeverity: string;
};

export function useGitgoData(
  client: McpClient | null,
  refreshSec: number = 5
) {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const { loading, error, run } = useAsyncPoll(true);

  const fetchData = async () => {
    if (!client) return;
    await run(async () => {
      const raw: unknown = await listProjects(client);
      const projectList: ProjectInfo[] = Array.isArray(raw) ? raw : [];

      const rows: ProjectRow[] = [];
      for (const p of projectList) {
        try {
          // Only call fast tools (disk reads, no file scanning)
          const [lessons, contract, statusData] = await Promise.all([
            lessonList(client, p.name).catch(() => ({ pending: [] })),
            contractShow(client, p.name).catch(() => null),
            loopStatus(client, p.name).catch(() => null),
          ]);

          const procs = statusData?.processes || {};
          const activeCount = Object.values(procs).filter(
            (x: any) => x.status === "running"
          ).length;
          const waitingCount = Object.values(procs).filter(
            (x: any) => x.status === "waiting"
          ).length;

          // Derive governance + LLM from loop status
          const providers = (statusData?.providers || []) as any[];
          let hasOpen = false;
          let hasHalfOpen = false;
          for (const pr of providers) {
            if (pr.breaker_state === "open") hasOpen = true;
            if (pr.breaker_state === "half_open") hasHalfOpen = true;
          }
          const govStatus = hasOpen ? "Open" : hasHalfOpen ? "Warning" : "Normal";
          const providerCount = providers.length;
          const llmSummary = providerCount > 0 ? `${providerCount} providers` : "Not configured";
          const llmStatus: StatusState = providerCount > 0 ? "ok" : "error";

          // Derive lessons severity
          const pendingLessons = lessons?.pending || [];
          let lessonsSev = "none";
          for (const l of pendingLessons) {
            if (l.severity === "critical") { lessonsSev = "high"; break; }
            if (l.severity === "high") lessonsSev = "med";
          }

          rows.push({
            ...p,
            pendingLessons: pendingLessons.length,
            features: contract?.decided_features?.length || 0,
            constraints: contract?.architecture_constraints?.length || 0,
            techStack: contract?.tech_stack?.join(", ") || "-",
            daemonOnline: statusData?.daemon_online ?? false,
            activeProcessCount: activeCount,
            waitingProcessCount: waitingCount,
            governanceStatus: govStatus,
            llmProviderSummary: llmSummary,
            llmStatus,
            lessonsSeverity: lessonsSev,
          });
        } catch {
          rows.push({ ...p, pendingLessons: 0, features: 0, constraints: 0, techStack: "?", daemonOnline: false, activeProcessCount: 0, waitingProcessCount: 0, governanceStatus: "?", llmProviderSummary: "?", llmStatus: "offline", lessonsSeverity: "none" });
        }
      }

      // Group: running → pending → finished, each sorted alphabetically.
      const { flat } = partitionByRank(
        rows,
        (r) =>
          r.daemonOnline && r.activeProcessCount > 0 ? 0
          : r.daemonOnline && r.waitingProcessCount > 0 ? 1 : 2,
        (r) => r.name,
      );

      // Atomic update — no intermediate clear
      setProjects(flat);
    }, { loading: false });
  };

  usePoll(fetchData, refreshSec * 1000, [client]);

  return { projects, loading, error, refresh: fetchData };
}
