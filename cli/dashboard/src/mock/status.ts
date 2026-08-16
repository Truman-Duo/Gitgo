// src/mock/status.ts — mock status-command data (running + completed scenarios).
export const MOCK_STATUS = {
  stage: "development",
  workspace: { entries_changed: 12, entries_total: 85 },
  commits: { formal_synced: 34, formal_total: 34 },
  semantic: { suggested_next_action: "continue_implementation" },
};

export const MOCK_STATUS_COMPLETED = {
  stage: "complete",
  workspace: { entries_changed: 0, entries_total: 64 },
  commits: { formal_synced: 41, formal_total: 41 },
  semantic: { suggested_next_action: "idle" },
};
