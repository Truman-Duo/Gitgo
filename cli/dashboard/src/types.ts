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

export type ToolState = "pending" | "running" | "completed" | "error" | "unresolved";

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
  /** Deterministic Host projection used when verbose output is folded. */
  compact_summary?: string;
  is_running?: boolean;
  state?: ToolState;
  diff?: string;
  /** Human-facing source used when A projects work performed by a B. */
  actor_label?: "A" | "B" | string;
  process_id?: string;
  detail_ref?: string;
};

export type RuntimeNoticeSeverity = "info" | "success" | "warning" | "error";

/** Compact, deterministic projection of Host runtime events for the main chat. */
export type RuntimeNotice = {
  key: string;
  kind: string;
  label: string;
  detail?: string;
  severity: RuntimeNoticeSeverity;
  timestamp?: string;
  process_id?: string;
  detail_ref?: string;
  actor_label?: "A" | "B" | string;
};

export type ProviderUsageSummary = {
  input_tokens: number;
  output_tokens: number;
};

export type StreamActivity =
  | { kind: "reasoning"; text: string; process_id?: string; actor_label?: string }
  | { kind: "progress"; text: string; visibility: "public" | "verbose"; process_id?: string; actor_label?: string }
  | { kind: "tool"; toolIndex: number; detail_ref?: string }
  | { kind: "notice"; noticeIndex: number; detail_ref?: string };

export type DecisionOption = {
  label: string;
  principle: string;
  immediate_effect: string;
  downstream_effect: string;
  risks: string;
  reversibility: string;
  recommended: boolean;
  action?: string;
};

/** Full identity is mandatory: a decision is never inferred from project alone. */
export type PendingDecision = {
  task_id: string;
  process_id: string;
  decision_id: string;
  kind?: "clarification" | "preference" | "direction" | "verification" | "choice" | "permission" | "recovery" | "checkpoint" | string;
  state_topic?: string;
  supersedes_decision_id?: string;
  permission_request?: {
    purpose?: string;
    tool_name?: string;
    effect?: string;
    resource?: string;
    api_details?: string;
    arguments_preview?: string;
    approval_scope?: string;
  };
  question: string;
  why_user_must_decide: string;
  options: DecisionOption[];
  allow_free_form: boolean;
  created_at?: string;
  source_process_id?: string;
  source_actor_kind?: string;
  source_display_name?: string;
  owner_process_id?: string;
};

export type ChatMessage = {
  decision?: PendingDecision;
  decision_answer?: string | null;
  role: "system" | "user" | "assistant";
  content: string;
  timestamp: string;
  /** Stable backend identity. Optimistic rows use an `optimistic:` prefix. */
  message_id?: string;
  turn_id?: string;
  kind?: "conversation" | "outcome" | "error" | string;
  visibility?: "public" | "verbose" | string;
  pending?: boolean;
  awaiting_persistence?: boolean;
  final?: boolean;
  /** Canonical terminal status of this submitted turn. */
  status?: "completed" | "awaiting_user" | "degraded" | "failed" |
    "cancelled" | "timed_out" | string;
  /** End-to-end turn duration reported by the Host, not a UI stopwatch. */
  duration_ms?: number;
  /** Task trace identity used for lazy trajectory materialization. */
  trace_id?: string;
  /** Process identity used to exclude sibling-subprocess events from a task-tree trace. */
  process_id?: string;
  /** Plaintext provider reasoning retained by the explicit product policy. */
  reasoning?: string;
  /** Ordered live projection retained while the SQLite trace becomes authoritative. */
  activity?: StreamActivity[];
  id?: string;
  tools?: ToolCallCard[];
  notices?: RuntimeNotice[];
  provider_usage?: ProviderUsageSummary;
};

// Transient streaming row — kept OUT of the persisted message list so it is
// never clobbered by the authoritative poll snapshot.
export type StreamingRow = {
  text: string;
  reasoning: string;
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  activity?: StreamActivity[];
  timestamp: string;
  visibility?: "public" | "verbose";
  providerStep?: number;
  provider_usage?: ProviderUsageSummary;
};

// Imperative scroll handle exposed by the chat ScrollBox, registered upward
// (mirrors sendChatRef) so the keymap can drive scroll without React state.
export type ChatScrollHandle = {
  scrollBy: (dy: number) => void;
  scrollToBottom: () => void;
};
