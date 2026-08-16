// src/hooks/useLoopData.ts
// Polls gitgo_loop_status every 5s with 16ms event batching (borrowed from OpenCode sdk.tsx)

import { useState, useEffect, useRef, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";
import type { ChatMessage } from "../types.js";
import { getDaemonClient } from "../clients.js";
import { usePoll } from "./usePoll.js";
import { loopStatus } from "../mcp/tools.js";

export type ProcessInfo = {
  process_id: string;
  role: string;
  ring_level: number;
  status: string;
  steps_used: number;
  max_steps: number;
  parent_id: string | null;
  created_at: string;
  worktree_path: string;
  provider_id: string;
  model_id: string;
  estimated_tokens: number;
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
};

export function useLoopData(
  client: McpClient | null,
  project: string | null,
  refreshSec: number = 5,
): LoopData & { refresh: () => Promise<void> } {
  const [processes, setProcesses] = useState<Record<string, ProcessInfo>>({});
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [daemonOnline, setDaemonOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mainConversation, setMainConversation] = useState<ChatMessage[] | undefined>(undefined);
  const [agentConversations, setAgentConversations] = useState<Record<string, ChatMessage[]> | undefined>(undefined);

  // 16ms batch window (OpenCode pattern)
  const batchRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<{
    processes: Record<string, ProcessInfo>;
    toolEvents: ToolEvent[];
    providers: ProviderHealth[];
    daemonOnline: boolean;
    mainConversation: ChatMessage[] | undefined;
    agentConversations: Record<string, ChatMessage[]> | undefined;
  } | null>(null);

  const flushBatch = useCallback(() => {
    if (!pendingRef.current) return;
    const p = pendingRef.current;
    setProcesses(p.processes);
    setToolEvents(p.toolEvents);
    setProviders(p.providers);
    setDaemonOnline(p.daemonOnline);
    setMainConversation(p.mainConversation);
    setAgentConversations(p.agentConversations);
    setError(null);
    pendingRef.current = null;
  }, []);

  const fetchData = useCallback(async () => {
    if (!client || !project) return;
    try {
      // Prefer native daemon; fall back to MCP
      const daemon = getDaemonClient();
      const caller = (daemon?.ready ? daemon : client) as McpClient;
      const result: any = await loopStatus(caller, project);
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
        providers: (result?.providers || []) as ProviderHealth[],
        daemonOnline: result?.daemon_online ?? false,
        mainConversation: (result?.main_conversation || undefined) as ChatMessage[] | undefined,
        agentConversations: (result?.agent_conversations || undefined) as Record<string, ChatMessage[]> | undefined,
      };
      if (batchRef.current) clearTimeout(batchRef.current);
      batchRef.current = setTimeout(flushBatch, 16);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [client, project, flushBatch]);

  usePoll(fetchData, refreshSec * 1000, [fetchData, refreshSec]);

  // Ensure batchRef is cleaned up on unmount
  useEffect(() => {
    return () => {
      if (batchRef.current) clearTimeout(batchRef.current);
    };
  }, []);

  return {
    processes,
    toolEvents,
    providers,
    daemonOnline,
    loading,
    error,
    mainConversation,
    agentConversations,
    refresh: fetchData,
  };
}
