// src/types.ts — Shared types consumed by hooks, components, and mock data.

// ── Diff (OpenCode-style side-by-side / unified) ────────────

export type DiffLine = {
  type: "add" | "remove" | "context";
  text: string;
};

export type DiffHunk = {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: DiffLine[];
};

export type FileDiff = {
  file: string;
  additions: number;
  deletions: number;
  status: "added" | "modified" | "deleted";
  hunks: DiffHunk[];
};

// ── Tool call lifecycle ─────────────────────────────────────

export type ToolState = "pending" | "running" | "completed" | "error";

export type ToolCallCard = {
  tool_name: string;
  tool_call_id?: string;
  target: string;
  allowed: boolean;
  status_label: string;
  duration_ms: number;
  timestamp: string;
  blocked_reason?: string;
  result_text?: string;
  is_running?: boolean;
  state?: ToolState;
  diff?: string;
};

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
  timestamp: string;
  pending?: boolean;
  final?: boolean;
  id?: string;
  tools?: ToolCallCard[];
};

// Transient streaming row — kept OUT of the persisted message list so it is
// never clobbered by the authoritative poll snapshot.
export type StreamingRow = {
  text: string;
  tools: ToolCallCard[];
  timestamp: string;
};

// Imperative scroll handle exposed by the chat ScrollBox, registered upward
// (mirrors sendChatRef) so the keymap can drive scroll without React state.
export type ChatScrollHandle = {
  scrollBy: (dy: number) => void;
  scrollToBottom: () => void;
};
