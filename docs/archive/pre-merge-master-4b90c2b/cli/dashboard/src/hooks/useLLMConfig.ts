// src/hooks/useLLMConfig.ts
// Fetch and manage LLM provider configuration via MCP tools.

import { useState, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";

export type LLMProvider = {
  id: string;
  name: string;
  base_url: string;
  api_key: string;
  model_id: string;
  created_at: string;
};

export type LLMConfigState = {
  providers: LLMProvider[];
  active_provider: string;
  failover_enabled: boolean;
  failover_order: string[];
  loading: boolean;
  error: string | null;
};

export function useLLMConfig(client: McpClient | null, project: string | null) {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [activeProvider, setActiveProvider] = useState("");
  const [failoverEnabled, setFailoverEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    if (!client || !project) return;
    setLoading(true);
    try {
      const result: any = await client.callTool("gitgo_llm_status", { project });
      if (result?.error) { setError(result.error); return; }
      setProviders((result?.providers || []) as LLMProvider[]);
      setActiveProvider(result?.active_provider || "");
      setFailoverEnabled(result?.failover_enabled || false);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [client, project]);

  const saveProvider = useCallback(async (p: LLMProvider) => {
    if (!client || !project) return null;
    try {
      const result: any = await client.callTool("gitgo_llm_save", {
        project,
        provider_id: p.id || "",
        name: p.name,
        base_url: p.base_url,
        api_key: p.api_key,
        model_id: p.model_id,
      });
      if (result?.error) { setError(result.error); return null; }
      await fetchStatus(); // refresh list
      return result;
    } catch (e: any) {
      setError(e.message);
      return null;
    }
  }, [client, project, fetchStatus]);

  const switchProvider = useCallback(async (providerId: string) => {
    if (!client || !project) return false;
    try {
      const result: any = await client.callTool("gitgo_llm_switch", {
        project, provider_id: providerId,
      });
      if (result?.error) { setError(result.error); return false; }
      setActiveProvider(providerId);
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    }
  }, [client, project]);

  const deleteProvider = useCallback(async (providerId: string) => {
    if (!client || !project) return false;
    try {
      const result: any = await client.callTool("gitgo_llm_delete", {
        project, provider_id: providerId,
      });
      if (result?.error) { setError(result.error); return false; }
      await fetchStatus();
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    }
  }, [client, project, fetchStatus]);

  return {
    providers,
    activeProvider,
    failoverEnabled,
    loading,
    error,
    fetchStatus,
    saveProvider,
    switchProvider,
    deleteProvider,
  };
}
