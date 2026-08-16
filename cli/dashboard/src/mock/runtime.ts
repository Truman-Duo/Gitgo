// src/mock/runtime.ts — mock runtime-command data (history / trial / formal / memory / templates).
// These are module-private inputs to MCP_MOCK_MAP (registry.ts); not part of the public MOCK_* surface.

export const MOCK_HISTORY_ENTRIES = [
  { timestamp: "2026-08-13T08:04:00Z", operation: "write_file", status: "ok" },
  { timestamp: "2026-08-13T08:03:00Z", operation: "write_file", status: "blocked" },
  { timestamp: "2026-08-13T07:10:00Z", operation: "formal_sync", status: "ok" },
  { timestamp: "2026-08-13T07:02:00Z", operation: "edit", status: "ok" },
];

export const MOCK_TRIAL_INCOMING = [
  { index: 0, hash: "a1b2c3d4e5f6", message: "Add health-check-endpoint to API gateway", author: "coder", date: "2026-08-13T08:10:00Z", triage: "incoming" },
  { index: 1, hash: "f7e8d9c0b1a2", message: "Refactor logging-pipeline to structured format", author: "coder", date: "2026-08-13T07:15:00Z", triage: "incoming" },
];

export const MOCK_FORMAL_COMMITS = [
  { index: 0, prefix: "ERGO", number: 34, message: "feat: add db-migrations", synced: true, pushed: true, is_incoming: false, created_at: "2026-08-12T18:00:00Z" },
  { index: 1, prefix: "ERGO", number: 33, message: "fix: circuit-breaker half-open recovery", synced: true, pushed: false, is_incoming: false, created_at: "2026-08-11T14:00:00Z" },
  { index: 2, prefix: "ERGO", number: 32, message: "chore: bump config-hot-reload", synced: false, pushed: false, is_incoming: true, created_at: "2026-08-11T10:00:00Z" },
];

export const MOCK_MEMORY_SNAPSHOTS = [
  { source: "knowledge", timestamp: "2026-08-13T07:10:00Z", path: "/home/gitgo/ergo/.gitgo/memory/knowledge", is_dir: true },
  { source: "lessons", timestamp: "2026-08-12T18:00:00Z", path: "/home/gitgo/ergo/.gitgo/memory/lessons", is_dir: true },
];

export const MOCK_TEMPLATES = [
  { name: "conventional", description: "type(scope): subject — conventional commits" },
  { name: "release", description: "Release commit with changelog summary" },
  { name: "hotfix", description: "Urgent fix with rollback note" },
  { name: "wip", description: "Work-in-progress marker" },
];
