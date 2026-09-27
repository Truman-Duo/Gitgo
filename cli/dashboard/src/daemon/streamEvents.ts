// src/daemon/streamEvents.ts — typed streaming events emitted by the native daemon.
// These replace the untyped `any` payloads that useChat previously matched on
// string literals. Keep the union in one place so the transport and the reducer
// agree on the shape of each event.

export type TextDeltaEvent = {
  event: "text_delta";
  delta: string;
  process_id: string;
  step?: number;
  visibility?: "public" | "verbose";
};

export type ReasoningDeltaEvent = {
  event: "reasoning_delta";
  delta: string;
  process_id: string;
  step?: number;
};

export type ProgressSummaryEvent = {
  event: "progress_summary";
  message: string;
  phase?: string;
  process_id: string;
  step?: number;
};

export type ToolcallStartEvent = {
  event: "toolcall_start";
  tool_name: string;
  tool_call_id?: string;
  target?: string;
  process_id: string;
  time?: string;
  detail_ref?: string;
};

export type ToolcallDeltaEvent = {
  event: "toolcall_delta";
  tool_call_id?: string;
  tool_name?: string;
  delta?: string;
  process_id: string;
};

export type ToolProgressEvent = {
  event: "tool_progress";
  tool_call_id?: string;
  tool_name?: string;
  status: string;
  reason?: string;
  process_id: string;
};

export type StreamRecoveryEvent = {
  event: "stream_recovery";
  attempt: number;
  max: number;
  process_id: string;
};

export type ToolResultEvent = {
  event: "tool_result";
  tool_call_id?: string;
  tool_name: string;
  is_error: boolean;
  error?: string;
  result_preview?: string;
  compact_summary?: string;
  diff?: string;
  duration_ms?: number;
  process_id: string;
  time?: string;
  detail_ref?: string;
};

export type CompositeStepResultEvent = {
  event: "composite_step_result";
  execution_id: string;
  composite_tool: string;
  step_id: string;
  tool_name: string;
  is_error: boolean;
  error?: string;
  duration_ms?: number;
  receipt?: { receipt_id?: string; committed?: boolean };
  process_id: string;
  time?: string;
};

export type TraceLifecycleEvent = {
  event: "provider_request_started" | "provider_response_completed" |
    "provider_response_incomplete" | "provider_usage" |
    "provider_capability_fallback" |
    "governance_snapshot" | "context_window_action" |
    "context_compaction_completed" | "completion_gate" | "agent_started" |
    "agent_terminal" | "agent_complete" | "task_bundle_delegated" |
    "worktree_leased" | "worktree_sealed" | "worktree_promoted" |
    "worktree_cleanup_failed" |
    "multi_agent_routed_to_supervisor" | "agent_dag_admitted" |
    "toolcall_done" | "decision_required" | "sessions_recovery_available" |
    "session_recovery_resumed" | "session_recovery_discarded" |
    "session_recovery_blocked" |
    "deadline_extended" |
    "coordination_event" | "coordination_event_resolved" |
    "coordination_observation_failed" |
    "storage_health" | "repository_scope_blocked" | "repository_scope_warning" |
    "task_admitted" | "mailbox_applied" | "user_decision_received" |
    "decision_resumed" | "context_compaction_requested" |
    "context_compaction_failed" | "stream_recovery" | "progress_summary" |
    "lessons_harvested" | "lesson_harvest_failed" | "lessons_verified" | "lessons_discarded" |
    "provider_switched";
  process_id?: string;
  step?: number;
  task_kind?: string;
  time?: string;
  [key: string]: unknown;
};

export type StreamEvent =
  | TextDeltaEvent
  | ReasoningDeltaEvent
  | ProgressSummaryEvent
  | ToolcallStartEvent
  | ToolcallDeltaEvent
  | ToolProgressEvent
  | StreamRecoveryEvent
  | ToolResultEvent
  | CompositeStepResultEvent
  | TraceLifecycleEvent;
