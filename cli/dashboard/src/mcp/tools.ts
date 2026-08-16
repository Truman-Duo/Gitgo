// src/mcp/tools.ts — Typed MCP tool wrappers.
// Single source of truth for every gitgo_* tool name + arg shape.
import type { McpClient } from "./client.js";

// ── Project management ────────────────────────────────────

export function listProjects(client: McpClient): Promise<any> {
  return client.callTool("gitgo_list_projects");
}

export function createProject(
  client: McpClient,
  params: { name: string; workspace_path: string; release_url?: string; llm_provider?: string }
): Promise<any> {
  return client.callTool("gitgo_create_project", params as Record<string, any>);
}

export function listArchivedProjects(client: McpClient): Promise<any> {
  return client.callTool("gitgo_list_archived_projects");
}

export function archiveProject(client: McpClient, name: string): Promise<any> {
  return client.callTool("gitgo_archive_project", { name });
}

export function deleteProject(
  client: McpClient, name: string, mode: "soft" | "hard"
): Promise<any> {
  return client.callTool("gitgo_delete_project", { name, mode });
}

export function cancelPendingDelete(client: McpClient, name: string): Promise<any> {
  return client.callTool("gitgo_cancel_pending_delete", { name });
}

// ── Runtime: Lessons ──────────────────────────────────────

export function lessonList(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_lesson_list", { project });
}

export function lessonSearch(client: McpClient, project: string, query: string): Promise<any> {
  return client.callTool("gitgo_lesson_search", { project, query });
}

export function lessonVerify(client: McpClient, project: string, lessonId: string): Promise<any> {
  return client.callTool("gitgo_lesson_verify", { project, lesson_id: lessonId });
}

// ── Runtime: Contract ─────────────────────────────────────

export function contractShow(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_contract_show", { project });
}

// ── Runtime: Governance ───────────────────────────────────

export function governanceQuality(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_governance_quality", { project });
}

export function governancePatterns(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_governance_patterns", { project });
}

export function governanceFeed(client: McpClient, project: string, limit?: number): Promise<any> {
  return client.callTool("gitgo_governance_feed", { project, limit: limit ?? 20 });
}

export function governanceReleases(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_governance_releases", { project });
}

// ── Runtime: Memory ───────────────────────────────────────

export function memorySnapshot(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_memory_snapshot", { project });
}

export function memoryList(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_memory_list", { project });
}

export function memoryRestore(client: McpClient, project: string, ts: string): Promise<any> {
  return client.callTool("gitgo_memory_restore", { project, ts });
}

// ── Runtime: History ──────────────────────────────────────

export function historyList(client: McpClient, project: string, limit?: number): Promise<any> {
  return client.callTool("gitgo_history", { project, limit: limit ?? 20 });
}

// ── Runtime: Trial ────────────────────────────────────────

export function trialList(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_trial_list", { project });
}

export function trialTriage(
  client: McpClient, project: string, index: number, action: string
): Promise<any> {
  return client.callTool("gitgo_trial_triage", { project, index, action });
}

// ── Runtime: Formal ───────────────────────────────────────

export function formalList(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_formal_list", { project });
}

export function formalEditMessage(
  client: McpClient, project: string, index: number, message: string
): Promise<any> {
  return client.callTool("gitgo_formal_edit_message", { project, index, message });
}

export function formalDelete(client: McpClient, project: string, index: number): Promise<any> {
  return client.callTool("gitgo_formal_delete", { project, index });
}

export function formalDissolve(client: McpClient, project: string, index: number): Promise<any> {
  return client.callTool("gitgo_formal_dissolve", { project, index });
}

// ── Loop / Agent ──────────────────────────────────────────

export function loopStatus(client: McpClient, project: string): Promise<any> {
  return client.callTool("gitgo_loop_status", { project });
}

export function agentChat(client: McpClient, project: string, message: string): Promise<any> {
  return client.callTool("gitgo_agent_chat", { project, message });
}

export function stopProcess(client: McpClient, project: string, processId: string): Promise<any> {
  return client.callTool("gitgo_stop_process", { project, process_id: processId });
}

// ── Config ────────────────────────────────────────────────

export function configGet(client: McpClient): Promise<any> {
  return client.callTool("gitgo_config_get");
}

export function configSet(client: McpClient, key: string, value: any): Promise<any> {
  return client.callTool("gitgo_config_set", { key, value });
}

// ── Templates ─────────────────────────────────────────────

export function templateList(client: McpClient): Promise<any> {
  return client.callTool("gitgo_template_list");
}

export function templateAdd(
  client: McpClient,
  params: { name: string; description: string; header_format?: string; body_format?: string }
): Promise<any> {
  return client.callTool("gitgo_template_add", params as Record<string, any>);
}

export function templateEdit(
  client: McpClient, name: string, description: string
): Promise<any> {
  return client.callTool("gitgo_template_edit", { name, description });
}

export function templateDelete(client: McpClient, name: string): Promise<any> {
  return client.callTool("gitgo_template_delete", { name });
}

// ── LLM Config ────────────────────────────────────────────

export function llmStatus(client: McpClient): Promise<any> {
  return client.callTool("gitgo_llm_status");
}

export function llmSave(
  client: McpClient,
  provider: { id?: string; name: string; base_url: string; api_key: string; model_id: string }
): Promise<any> {
  return client.callTool("gitgo_llm_save", {
    provider_id: provider.id || "",
    name: provider.name,
    base_url: provider.base_url,
    api_key: provider.api_key,
    model_id: provider.model_id,
  });
}

export function llmSwitch(client: McpClient, providerId: string): Promise<any> {
  return client.callTool("gitgo_llm_switch", { provider_id: providerId });
}

export function llmDelete(client: McpClient, providerId: string): Promise<any> {
  return client.callTool("gitgo_llm_delete", { provider_id: providerId });
}

// ── Export ────────────────────────────────────────────────

export function exportData(
  client: McpClient,
  project: string,
  scope: { minimal: boolean; include_identity?: boolean }
): Promise<any> {
  return client.callTool("gitgo_export", {
    project,
    minimal: scope.minimal,
    include_identity: !scope.minimal,
  });
}
