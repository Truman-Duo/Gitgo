// src/mock/providers.ts — mock LLM provider data (health + full config + completed).
import type { ProviderHealth } from "../hooks/useLoopData.js";
import type { LLMProvider } from "../hooks/useLLMConfig.js";

// ── ProviderHealth (for LLMConfig / ProcessList status tab) ──

export const MOCK_PROVIDERS: ProviderHealth[] = [
  { id: "prov-groq", breaker_state: "closed", failures: 0, available: true },
  { id: "prov-openai", breaker_state: "half_open", failures: 3, available: true },
  { id: "prov-anthropic", breaker_state: "open", failures: 7, available: false },
];

// ── LLMProvider (full config, for ConfigPanel) ────────────

export const MOCK_LLM_PROVIDERS: LLMProvider[] = [
  {
    id: "prov-groq",
    name: "Groq Llama 3.3",
    base_url: "https://api.groq.com/openai/v1",
    api_key: "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    model_id: "llama-3.3-70b-versatile",
    created_at: "2026-06-15T10:00:00Z",
  },
  {
    id: "prov-openai",
    name: "OpenAI GPT-4o",
    base_url: "https://api.openai.com/v1",
    api_key: "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    model_id: "gpt-4o",
    created_at: "2026-06-20T14:00:00Z",
  },
  {
    id: "prov-anthropic",
    name: "Anthropic Claude",
    base_url: "https://api.anthropic.com/v1",
    api_key: "sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    model_id: "claude-sonnet-4-6",
    created_at: "2026-07-01T09:00:00Z",
  },
];

// ── Completed-project provider health (atlas / forge) ────────

export const MOCK_PROVIDERS_COMPLETED: ProviderHealth[] = [
  { id: "prov-groq", breaker_state: "closed", failures: 0, available: true },
  { id: "prov-openai", breaker_state: "closed", failures: 0, available: true },
  { id: "prov-anthropic", breaker_state: "closed", failures: 0, available: true },
];
