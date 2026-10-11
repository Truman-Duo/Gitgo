// src/hooks/useLLMConfig.ts
// Fetch and manage LLM provider configuration through native application services.

import { useState, useCallback } from "react";
import type { BackendClient } from "../backend/client.js";
import { useAsyncPoll } from "./useAsyncPoll.js";
import { llmStatus, llmSave, llmSwitch, llmDelete, llmFailoverConfigure, loopStatus } from "../backend/tools.js";
import { sortByName } from "../theme/index.js";

export type LLMProvider = {
  id: string;
  name: string;
  base_url: string;
  api_key: string;
  api_key_present: boolean;
  api_key_display: string;
  model_id: string;
  protocol?: "openai_chat" | "openai_responses" | "anthropic_messages" | "auto";
  capabilities?: Record<string, any>;
  context_window: number;
  max_output_tokens: number;
  limits_source?: "default" | "configured";
  created_at: string;
  kind?: "remote" | "local";
  auth_mode?: "api_key" | "none";
};

export type LLMConfigState = {
  providers: LLMProvider[];
  active_provider: string;
  failover_enabled: boolean;
  failover_order: string[];
  loading: boolean;
  error: string | null;
};

export function useLLMConfig(client: BackendClient | null, project = "") {
  const [providers, setProviders] = useState<LLMProvider[]>([]);
  const [activeProvider, setActiveProvider] = useState("");
  const [failoverEnabled, setFailoverEnabled] = useState(false);
  const [failoverOrder, setFailoverOrder] = useState<string[]>([]);
  const [health, setHealth] = useState<Record<string, {breaker_state: string; failures: number | null}>>({});
  const { loading, error, run, setError } = useAsyncPoll(true);

  const fetchStatus = useCallback(async () => {
    if (!client) return;
    await run(async () => {
      const result: any = await llmStatus(client);
      if (result?.error) throw new Error(String(result.error));
      setProviders(sortByName((result?.providers || []) as LLMProvider[]));
      setActiveProvider(result?.active_provider || "");
      setFailoverEnabled(Boolean(result?.failover_enabled));
      setFailoverOrder(result?.failover_order || []);
      setError(null);
      if (project) {
        const status: any = await loopStatus(client, project);
        setHealth(Object.fromEntries((status?.providers || []).map((p: any) => [p.id, p])));
      }
    });
  }, [client, project, run, setError]);

  const saveProvider = useCallback(async (p: LLMProvider) => {
    if (!client) return null;
    try {
      const result: any = await llmSave(client, {
        id: p.id || "",
        name: p.name,
        base_url: p.base_url,
        api_key: p.api_key,
        model_id: p.model_id,
        protocol: p.protocol || "openai_chat",
        context_window: p.context_window || 128000,
        max_output_tokens: p.max_output_tokens || 4096,
        retain_api_key: Boolean(p.id && !p.api_key),
        kind: p.kind || "remote", auth_mode: p.auth_mode || "api_key",
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
      await fetchStatus();
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    }
  }, [client, fetchStatus, setError]);

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

  const configureFailover = useCallback(async (enabled: boolean, order: string[]) => {
    if (!client) return false;
    try {
      const result: any = await llmFailoverConfigure(client, enabled, order);
      if (result?.error) { setError(result.error); return false; }
      await fetchStatus();
      return true;
    } catch (e: any) { setError(e.message); return false; }
  }, [client, fetchStatus, setError]);

  return {
    providers,
    activeProvider,
    failoverEnabled, failoverOrder, configureFailover, health,
    loading,
    error,
    fetchStatus,
    saveProvider,
    switchProvider,
    deleteProvider,
  };
}
