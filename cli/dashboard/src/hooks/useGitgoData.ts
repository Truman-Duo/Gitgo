// src/hooks/useGitgoData.ts
import { useState, useEffect, useRef } from "react";
import { McpClient } from "../mcp/client.js";

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
};

export function useGitgoData(
  client: McpClient | null,
  refreshSec: number = 5
) {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(false);

  const fetchData = async () => {
    if (!client) return;
    try {
      const raw: unknown = await client.callTool("gitgo_list_projects");
      const projectList: ProjectInfo[] = Array.isArray(raw) ? raw : [];

      const rows: ProjectRow[] = [];
      for (const p of projectList) {
        try {
          // Only call fast tools (disk reads, no file scanning)
          const [lessons, contract, loopStatus] = await Promise.all([
            client.callTool("gitgo_lesson_list", { project: p.name }).catch(() => ({ pending: [] })),
            client.callTool("gitgo_contract_show", { project: p.name }).catch(() => null),
            client.callTool("gitgo_loop_status", { project: p.name }).catch(() => null),
          ]);

          const procs = loopStatus?.processes || {};
          const activeCount = Object.values(procs).filter(
            (x: any) => x.status === "running"
          ).length;

          rows.push({
            ...p,
            pendingLessons: lessons?.pending?.length || 0,
            features: contract?.decided_features?.length || 0,
            constraints: contract?.architecture_constraints?.length || 0,
            techStack: contract?.tech_stack?.join(", ") || "-",
            daemonOnline: loopStatus?.daemon_online ?? false,
            activeProcessCount: activeCount,
          });
        } catch {
          rows.push({ ...p, pendingLessons: 0, features: 0, constraints: 0, techStack: "?", daemonOnline: false, activeProcessCount: 0 });
        }
      }

      // Atomic update — no intermediate clear
      setProjects(rows);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    timerRef.current = setInterval(fetchData, refreshSec * 1000);
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [client]);

  return { projects, loading, error, refresh: fetchData };
}
