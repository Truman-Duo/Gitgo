// src/hooks/useLoopData.ts
// Polls gitgo_loop_status every 5s with 16ms event batching.
// Prefers native daemon client when available; falls back to MCP.

import { useState, useEffect, useRef, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";
import { getDaemonClient } from "../clients.js";

export type ProcessInfo = {
  process_id: string;
  role: string;
  ring_level: number;
  status: string;
  steps_used: number;
  max_steps: number;
  parent_id: string | null;
  created_at: string;
};

export type ToolEvent = {
  timestamp: string;
  process_id: string;
  tool_name: string;
  allowed: boolean;
  duration_ms: number;
  role: string;
};

export type LoopData = {
  processes: Record<string, ProcessInfo>;
  toolEvents: ToolEvent[];
  daemonOnline: boolean;
  loading: boolean;
  error: string | null;
};

export function useLoopData(
  client: McpClient | null,
  project: string | null,
  refreshSec: number = 5,
): LoopData & { refresh: () => Promise<void> } {
  const [processes, setProcesses] = useState<Record<string, ProcessInfo>>({});
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const [daemonOnline, setDaemonOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 16ms batch window (OpenCode pattern)
  const batchRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<{
    processes: Record<string, ProcessInfo>;
    toolEvents: ToolEvent[];
    daemonOnline: boolean;
  } | null>(null);

  const flushBatch = useCallback(() => {
    if (!pendingRef.current) return;
    const p = pendingRef.current;
    setProcesses(p.processes);
    setToolEvents(p.toolEvents);
    setDaemonOnline(p.daemonOnline);
    setError(null);
    pendingRef.current = null;
  }, []);

  const fetchData = useCallback(async () => {
    if (!client || !project) return;
    try {
      // Prefer native daemon; fall back to MCP
      const daemon = getDaemonClient();
      const caller = (daemon?.ready ? daemon : client) as McpClient;
      const result: any = await caller.callTool("gitgo_loop_status", {
        project,
      });
      const procs: Record<string, ProcessInfo> = {};
      if (result?.processes) {
        for (const [pid, p] of Object.entries(result.processes)) {
          procs[pid] = p as ProcessInfo;
        }
      }
      // Batch: enqueue and flush on 16ms window
      pendingRef.current = {
        processes: procs,
        toolEvents: (result?.recent_tool_executed || []) as ToolEvent[],
        daemonOnline: result?.daemon_online ?? false,
      };
      if (batchRef.current) clearTimeout(batchRef.current);
      batchRef.current = setTimeout(flushBatch, 16);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [client, project, flushBatch]);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, refreshSec * 1000);
    return () => {
      clearInterval(timer);
      if (batchRef.current) clearTimeout(batchRef.current);
    };
  }, [fetchData, refreshSec]);

  return {
    processes,
    toolEvents,
    daemonOnline,
    loading,
    error,
    refresh: fetchData,
  };
}
