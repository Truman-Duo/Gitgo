// src/daemon/streamEvents.ts — typed streaming events emitted by the native daemon.
// These replace the untyped `any` payloads that useChat previously matched on
// string literals. Keep the union in one place so the transport and the reducer
// agree on the shape of each event.

export type TextDeltaEvent = {
  event: "text_delta";
  delta: string;
  process_id: string;
};

export type ToolcallStartEvent = {
  event: "toolcall_start";
  tool_name: string;
  tool_call_id?: string;
  target?: string;
  process_id: string;
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

export type StreamEvent =
  | TextDeltaEvent
  | ToolcallStartEvent
  | ToolcallDeltaEvent
  | ToolProgressEvent
  | StreamRecoveryEvent;
