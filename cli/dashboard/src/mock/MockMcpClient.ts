// src/mock/MockMcpClient.ts — Mock MCP client for --mock mode
// Returns canned data from the mock registry, simulating a real MCP server.

import { MCP_MOCK_MAP } from "./index.js";

const NATIVE_TO_LEGACY: Record<string, string> = {
  "project.list": "gitgo_list_projects",
  "project.create": "gitgo_create_project",
  "project.list_archived": "gitgo_list_archived_projects",
  "project.archive": "gitgo_archive_project",
  "project.delete": "gitgo_delete_project",
  "project.cancel_delete": "gitgo_cancel_pending_delete",
  "lesson.list": "gitgo_lesson_list",
  "lesson.search": "gitgo_lesson_search",
  "lesson.verify": "gitgo_lesson_verify",
  "contract.show": "gitgo_contract_show",
  "governance.quality": "gitgo_governance_quality",
  "governance.patterns": "gitgo_governance_patterns",
  "governance.feed": "gitgo_governance_feed",
  "governance.releases": "gitgo_governance_releases",
  "memory.snapshot": "gitgo_memory_snapshot",
  "memory.list": "gitgo_memory_list",
  "memory.restore": "gitgo_memory_restore",
  "history.list": "gitgo_history",
  "trial.list": "gitgo_trial_list",
  "trial.triage": "gitgo_trial_triage",
  "formal.list": "gitgo_formal_list",
  "formal.edit_message": "gitgo_formal_edit_message",
  "formal.delete": "gitgo_formal_delete",
  "formal.dissolve": "gitgo_formal_dissolve",
  "runtime.status": "gitgo_loop_status",
  "runtime.chat": "gitgo_agent_chat",
  "runtime.stop": "gitgo_stop_process",
  "config.get": "gitgo_config_get",
  "config.set": "gitgo_config_set",
  "template.list": "gitgo_template_list",
  "template.add": "gitgo_template_add",
  "template.edit": "gitgo_template_edit",
  "template.delete": "gitgo_template_delete",
  "provider.status": "gitgo_llm_status",
  "provider.save": "gitgo_llm_save",
  "provider.switch": "gitgo_llm_switch",
  "provider.delete": "gitgo_llm_delete",
  "provider.test": "gitgo_llm_test",
  "project.export": "gitgo_export",
};

export class MockMcpClient {
  private _ready = true;

  get ready(): boolean {
    return this._ready;
  }

  async callTool(
    toolName: string,
    args: Record<string, any> = {},
  ): Promise<any> {
    // Simulate network latency (50-150ms)
    const delay = 50 + Math.random() * 100;
    await new Promise((r) => setTimeout(r, delay));

    const handler = MCP_MOCK_MAP[NATIVE_TO_LEGACY[toolName] || toolName];
    if (handler) {
      return handler(args);
    }
    return { error: `Unknown tool: ${toolName}` };
  }

  close() {
    this._ready = false;
  }
}
