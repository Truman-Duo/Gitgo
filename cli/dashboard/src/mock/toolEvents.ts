// src/mock/toolEvents.ts — mock tool-execution events (running + completed scenarios).
import type { ToolEvent } from "../hooks/useLoopData.js";

// ── ToolEvents (for Workspace / AgentDetail / EventsTab) ──────

export const MOCK_TOOL_EVENTS: ToolEvent[] = [
  {
    timestamp: "2026-08-13T08:01:00Z",
    process_id: "proc-001",
    tool_name: "read_file",
    allowed: true,
    duration_ms: 45,
    role: "architect",
  },
  {
    timestamp: "2026-08-13T08:02:00Z",
    process_id: "proc-001",
    tool_name: "grep",
    allowed: true,
    duration_ms: 120,
    role: "architect",
  },
  {
    timestamp: "2026-08-13T08:03:00Z",
    process_id: "proc-002",
    tool_name: "write_file",
    allowed: true,
    duration_ms: 230,
    role: "coder",
  },
  {
    timestamp: "2026-08-13T08:04:00Z",
    process_id: "proc-002",
    tool_name: "bash",
    allowed: true,
    duration_ms: 1500,
    role: "coder",
  },
  {
    timestamp: "2026-08-13T08:05:00Z",
    process_id: "proc-002",
    tool_name: "write_file",
    allowed: false,
    duration_ms: 0,
    role: "coder",
    blocked_reason: "security_policy: writes outside workspace",
  },
  {
    timestamp: "2026-08-13T08:06:00Z",
    process_id: "proc-001",
    tool_name: "dispatch_tool",
    allowed: true,
    duration_ms: 340,
    role: "architect",
  },
  {
    timestamp: "2026-08-13T08:12:00Z",
    process_id: "proc-003",
    tool_name: "read_file",
    allowed: true,
    duration_ms: 60,
    role: "reviewer",
  },
  {
    timestamp: "2026-08-13T07:01:00Z",
    process_id: "proc-004",
    tool_name: "glob",
    allowed: true,
    duration_ms: 80,
    role: "orchestrator",
  },
  {
    timestamp: "2026-08-13T07:02:00Z",
    process_id: "proc-004",
    tool_name: "edit",
    allowed: true,
    duration_ms: 95,
    role: "orchestrator",
  },
  {
    timestamp: "2026-08-13T07:15:00Z",
    process_id: "proc-005",
    tool_name: "bash",
    allowed: false,
    duration_ms: 0,
    role: "coder",
    blocked_reason: "identity_guard: 3 consecutive denials",
  },
  {
    timestamp: "2026-08-13T07:20:00Z",
    process_id: "proc-006",
    tool_name: "grep",
    allowed: true,
    duration_ms: 40,
    role: "debugger",
  },
];

// ── Completed-project tool events (atlas / forge) ────────────

export const MOCK_TOOL_EVENTS_COMPLETED: ToolEvent[] = [
  { timestamp: "2026-08-10T09:01:00Z", process_id: "cpl-001", tool_name: "read_file", allowed: true, duration_ms: 40, role: "orchestrator" },
  { timestamp: "2026-08-10T09:02:00Z", process_id: "cpl-001", tool_name: "dispatch_tool", allowed: true, duration_ms: 300, role: "orchestrator" },
  { timestamp: "2026-08-10T09:12:00Z", process_id: "cpl-002", tool_name: "write_file", allowed: true, duration_ms: 250, role: "coder" },
  { timestamp: "2026-08-10T09:13:00Z", process_id: "cpl-002", tool_name: "bash", allowed: true, duration_ms: 1200, role: "coder" },
  { timestamp: "2026-08-10T09:41:00Z", process_id: "cpl-003", tool_name: "read_file", allowed: true, duration_ms: 55, role: "reviewer" },
  { timestamp: "2026-08-10T09:42:00Z", process_id: "cpl-003", tool_name: "bash", allowed: true, duration_ms: 900, role: "reviewer" },
];
