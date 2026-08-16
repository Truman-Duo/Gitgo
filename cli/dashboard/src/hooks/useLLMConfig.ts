// src/hooks/useLLMConfig.ts
// Fetch and manage LLM provider configuration via MCP tools.

import { useState, useCallback } from "react";
import type { McpClient } from "../mcp/client.js";
import { useAsyncPoll } from "./useAsyncPoll.js";
import { llmStatus, llmSave, llmSwitch, llmDelete } from "../mcp/tools.js";
import { sortByName } from "../theme/index.js";

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

export function useLLMConfig(client: McpClient | null) {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [activeProvider, setActiveProvider] = useState("");
  const [failoverEnabled, setFailoverEnabled] = useState(false);
  const [failoverOrder, setFailoverOrder] = useState<string[]>([]);
  const { loading, error, run, setError } = useAsyncPoll(false);

  const fetchStatus = useCallback(async () => {
    if (!client) return;
    await run(async () => {
      const result: any = await llmStatus(client);
      if (result?.error) { setError(result.error); return; }
      setProviders(sortByName((result?.providers || []) as LLMProvider[]));
      setActiveProvider(result?.active_provider || "");
      setFailoverEnabled(result?.failover_enabled || false);
      setFailoverOrder((result?.failover_order || []) as string[]);
    });
  }, [client, run, setError]);

  const saveProvider = useCallback(async (p: LLMProvider) => {
    if (!client) return null;
    try {
      const result: any = await llmSave(client, {
        id: p.id || "",
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
  }, [client, fetchStatus, setError]);

  const switchProvider = useCallback(async (providerId: string) => {
    if (!client) return false;
    try {
      const result: any = await llmSwitch(client, providerId);
      if (result?.error) { setError(result.error); return false; }
      setActiveProvider(providerId);
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    }
  }, [client, setError]);

  const deleteProvider = useCallback(async (providerId: string) => {
    if (!client) return false;
    try {
      const result: any = await llmDelete(client, providerId);
      if (result?.error) { setError(result.error); return false; }
      await fetchStatus();
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    }
  }, [client, fetchStatus, setError]);

  const toggleFailover = useCallback(() => {
    setFailoverEnabled((prev) => !prev);
  }, []);

  return {
    providers,
    activeProvider,
    failoverEnabled,
    failoverOrder,
    loading,
    error,
    fetchStatus,
    saveProvider,
    switchProvider,
    deleteProvider,
    toggleFailover,
  };
}
