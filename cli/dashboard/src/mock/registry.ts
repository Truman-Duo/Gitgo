// src/mock/registry.ts — MCP tool name → mock data mapping.
import { MOCK_PROJECTS, MOCK_ARCHIVED } from "./projects.js";
import { MOCK_PROCESSES, MOCK_PROCESSES_COMPLETED } from "./processes.js";
import { MOCK_TOOL_EVENTS, MOCK_TOOL_EVENTS_COMPLETED } from "./toolEvents.js";
import { MOCK_PROVIDERS, MOCK_PROVIDERS_COMPLETED, MOCK_LLM_PROVIDERS } from "./providers.js";
import { MOCK_LESSONS } from "./lessons.js";
import { MOCK_CONTRACT, MOCK_CONTRACT_COMPLETED } from "./contract.js";
import { MOCK_STATUS, MOCK_STATUS_COMPLETED } from "./status.js";
import {
  MOCK_MAIN_CONVERSATION,
  MOCK_AGENT_CONVERSATIONS,
  MOCK_MAIN_CONVERSATION_COMPLETED,
  MOCK_AGENT_CONVERSATIONS_COMPLETED,
} from "./conversations.js";
import {
  MOCK_HISTORY_ENTRIES,
  MOCK_TRIAL_INCOMING,
  MOCK_FORMAL_COMMITS,
  MOCK_MEMORY_SNAPSHOTS,
  MOCK_TEMPLATES,
} from "./runtime.js";

export const MCP_MOCK_MAP: Record<string, (args: any) => any> = {
  gitgo_list_projects: () => MOCK_PROJECTS,

  gitgo_create_project: (args: { name?: string; workspace_path?: string }) => ({
    ok: true,
    name: args.name,
    workspace: args.workspace_path,
  }),

  gitgo_list_archived_projects: () => MOCK_ARCHIVED,

  gitgo_archive_project: () => ({ ok: true }),

  gitgo_delete_project: (args: { mode?: string }) => ({ ok: true, mode: args.mode }),

  gitgo_cancel_pending_delete: () => ({ ok: true }),

  gitgo_lesson_list: (args: { project?: string }) => {
    if (args.project === "nexus" || args.project === "atlas" || args.project === "forge") {
      return { pending: [] };
    }
    return MOCK_LESSONS;
  },

  gitgo_lesson_search: (args: { query?: string; project?: string }) => {
    if (args.project === "nexus" || args.project === "atlas" || args.project === "forge") {
      return { pending: [] };
    }
    const q = (args.query || "").toLowerCase();
    const pending = MOCK_LESSONS.pending.filter((l) =>
      !q || l.trigger.toLowerCase().includes(q) || l.severity.includes(q)
    );
    return { pending };
  },

  gitgo_lesson_verify: (args: { lesson_id?: string }) => ({
    verified_count: 1,
    lesson_id: args.lesson_id,
  }),

  gitgo_contract_show: (args: { project?: string }) => {
    if (args.project === "nexus") return { error: "no contract" };
    if (args.project === "atlas" || args.project === "forge") return MOCK_CONTRACT_COMPLETED;
    return MOCK_CONTRACT;
  },

  gitgo_governance_quality: () => ({
    score: 87,
    summary: "3 dimensions within threshold; 1 warning on failure rate",
    dimensions: [
      { name: "test_coverage", value: 0.92 },
      { name: "failure_rate", value: 0.04 },
      { name: "cycle_time", value: 1.6 },
    ],
  }),

  gitgo_governance_patterns: () => ({
    patterns: [
      { kind: "refactor", count: 14 },
      { kind: "feature", count: 9 },
      { kind: "fix", count: 6 },
    ],
  }),

  gitgo_governance_feed: (args: { limit?: number }) => ({
    events: MOCK_HISTORY_ENTRIES.slice(0, args.limit ?? 20),
  }),

  gitgo_governance_releases: () => ({
    releases: [
      { version: "v0.35.0", date: "2026-08-10", commits: 34 },
      { version: "v0.34.0", date: "2026-08-03", commits: 28 },
    ],
  }),

  gitgo_memory_snapshot: () => ({
    ok: true,
    snapshot_id: "snap-20260813-now",
    timestamp: "2026-08-13T09:00:00Z",
  }),

  gitgo_memory_list: () => ({ snapshots: MOCK_MEMORY_SNAPSHOTS }),

  gitgo_memory_restore: (args: { ts?: string }) => ({ ok: true, timestamp: args.ts }),

  gitgo_history: () => ({ events: MOCK_HISTORY_ENTRIES, entries: MOCK_HISTORY_ENTRIES }),

  gitgo_status: (args: { project?: string }) => {
    if (args.project === "nexus") return { stage: "offline", workspace: {}, commits: {} };
    if (args.project === "atlas" || args.project === "forge") return MOCK_STATUS_COMPLETED;
    return MOCK_STATUS;
  },

  gitgo_trial_list: () => ({ incoming: MOCK_TRIAL_INCOMING }),

  gitgo_trial_triage: (args: { index?: number; action?: string }) => ({
    ok: true,
    index: args.index,
    action: args.action,
  }),

  gitgo_formal_list: () => ({ formal_commits: MOCK_FORMAL_COMMITS }),

  gitgo_formal_edit_message: () => ({ ok: true }),

  gitgo_formal_delete: () => ({ ok: true }),

  gitgo_formal_dissolve: () => ({ ok: true }),

  gitgo_loop_status: (args: { project?: string }) => {
    if (args.project === "nexus") {
      return {
        processes: {},
        providers: [],
        daemon_online: false,
        recent_tool_executed: [],
        agent_conversations: {},
        main_conversation: [],
      };
    }
    if (args.project === "atlas" || args.project === "forge") {
      return {
        processes: MOCK_PROCESSES_COMPLETED,
        providers: MOCK_PROVIDERS_COMPLETED,
        daemon_online: true,
        recent_tool_executed: MOCK_TOOL_EVENTS_COMPLETED,
        agent_conversations: MOCK_AGENT_CONVERSATIONS_COMPLETED,
        main_conversation: MOCK_MAIN_CONVERSATION_COMPLETED,
      };
    }
    return {
      processes: MOCK_PROCESSES,
      providers: MOCK_PROVIDERS,
      daemon_online: true,
      recent_tool_executed: MOCK_TOOL_EVENTS,
      agent_conversations: MOCK_AGENT_CONVERSATIONS,
      main_conversation: MOCK_MAIN_CONVERSATION,
    };
  },

  gitgo_agent_chat: (args: { message?: string }) => ({
    response:
      `I've received: "${args.message}". ` +
      `I'll dispatch a coder B agent, run it through review, and report back once validated.`,
    process_id: "proc-001",
  }),

  gitgo_config_get: () => ({
    safety: { delete_delay_minutes: 5 },
    publish: { privacy_clean: true },
  }),

  gitgo_config_set: () => ({ ok: true }),

  gitgo_template_list: () => MOCK_TEMPLATES,

  gitgo_template_add: () => ({ ok: true }),

  gitgo_template_edit: () => ({ ok: true }),

  gitgo_template_delete: () => ({ ok: true }),

  gitgo_llm_status: (args: { project?: string }) => ({
    providers: MOCK_LLM_PROVIDERS,
    active_provider: "prov-groq",
    failover_enabled: true,
    failover_order: ["prov-groq", "prov-openai", "prov-anthropic"],
  }),

  gitgo_llm_save: (args: { provider_id?: string; name?: string }) => ({
    ok: true,
    provider_id: args.provider_id || args.name,
  }),

  gitgo_llm_switch: (args: { provider_id?: string }) => ({
    ok: true,
    active_provider: args.provider_id,
  }),

  gitgo_llm_delete: (args: { provider_id?: string }) => ({
    ok: true,
    provider_id: args.provider_id,
  }),

  gitgo_export: (args: { project?: string; minimal?: boolean }) => ({
    ok: true,
    export_path: `/tmp/gitgo/export/${args.project || "project"}-${args.minimal ? "minimal" : "full"}.json`,
    scope: args.minimal ? "minimal" : "full",
  }),
};
