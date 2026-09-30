// src/chat/sendChat.ts — transport orchestration for sending a chat message.
// Drives explicit mock mode or the production native host stream.
// and drives the transient streaming row + final assistant message via callbacks.
// `useChat` keeps only state and delegates the actual send to this module.
import type { BackendClient } from "../backend/client.js";
import { NativeHostClient } from "../backend/client.js";
import type {
  PendingDecision, RuntimeNotice, StreamingRow, ToolCallCard,
} from "../types.js";
import type { ToolEvent } from "../hooks/useLoopData.js";
import { agentChat, loopStatus } from "../backend/tools.js";
import { MockMcpClient } from "../mock/MockMcpClient.js";
import { simulateMockStream } from "../mock/mockStream.js";
import { initStreamState, reduceStreamEvent, finalizeTools } from "../daemon/streamReducer.js";
import { publishProcessStreamEvent } from "../daemon/processStreams.js";
import { reduceSupervisorEvent } from "./supervisorProjection.js";

export type ChatSendCallbacks = {
  onAcknowledged?: (ack: ChatAcknowledgement) => void;
  onStream: (row: StreamingRow) => void;
  onDone: (
    content: string,
    tools?: ToolCallCard[],
    notices?: RuntimeNotice[],
    pendingDecision?: PendingDecision | null,
    round?: CompletedRound,
  ) => void;
  onError: (message: string, round?: CompletedRound) => void;
};

export type ChatAcknowledgement = {
  requestId: string;
  processId: string;
  taskId: string;
  stage: string;
};

export type CompletedRound = {
  task_id: string;
  status: string;
  duration_ms: number;
  trace_id: string;
  process_id: string;
  reasoning?: string;
  activity?: StreamingRow["activity"];
  provider_usage?: StreamingRow["provider_usage"];
};

function completedRound(outcome: any, state = initStreamState()): CompletedRound {
  return {
    task_id: String(outcome?.task_id || ""),
    status: String(outcome?.status || "completed"),
    duration_ms: Number(outcome?.duration_ms || 0),
    trace_id: String(outcome?.metadata?.trace_id || outcome?.task_id || ""),
    process_id: String(outcome?.process_id || ""),
    reasoning: state.reasoning || undefined,
    activity: state.activity.length > 0 ? state.activity : undefined,
    provider_usage: state.providerUsage,
  };
}

function outcomeError(outcome: any): string {
  const code = outcome?.error?.code || "AGENT_TASK_FAILED";
  const message = outcome?.error?.message || `Agent task ended as ${outcome?.status || "unknown"}`;
  return `[${code}] ${message}`;
}

function pendingDecisionFromOutcome(outcome: any): PendingDecision | null {
  const raw = outcome?.metadata?.pending_decision;
  if (!raw || !raw.decision_id || !raw.process_id) return null;
  const options = Array.isArray(raw.options) ? raw.options : [];
  return {
    task_id: String(raw.task_id || outcome.task_id || ""),
    process_id: String(raw.process_id || outcome.process_id || ""),
    decision_id: String(raw.decision_id),
    kind: String(raw.kind || "choice"),
    state_topic: raw.state_topic ? String(raw.state_topic) : undefined,
    supersedes_decision_id: raw.supersedes_decision_id ? String(raw.supersedes_decision_id) : undefined,
    permission_request: raw.permission_request && typeof raw.permission_request === "object"
      ? {
          purpose: String(raw.permission_request.purpose || ""),
          tool_name: String(raw.permission_request.tool_name || ""),
          effect: String(raw.permission_request.effect || ""),
          resource: String(raw.permission_request.resource || ""),
          api_details: String(raw.permission_request.api_details || ""),
        }
      : undefined,
    question: String(raw.question || ""),
    why_user_must_decide: String(raw.why_user_must_decide || ""),
    options: options.map((option: any) => ({
      label: String(option?.label || ""),
      principle: String(option?.principle || ""),
      immediate_effect: String(option?.immediate_effect || ""),
      downstream_effect: String(option?.downstream_effect || ""),
      risks: String(option?.risks || ""),
      reversibility: String(option?.reversibility || ""),
      recommended: Boolean(option?.recommended),
      action: option?.action ? String(option.action) : undefined,
    })),
    allow_free_form: Boolean(raw.allow_free_form),
    created_at: raw.created_at ? String(raw.created_at) : undefined,
    source_process_id: raw.source_process_id ? String(raw.source_process_id) : undefined,
    source_actor_kind: raw.source_actor_kind ? String(raw.source_actor_kind) : undefined,
    source_display_name: raw.source_display_name ? String(raw.source_display_name) : undefined,
    owner_process_id: raw.owner_process_id ? String(raw.owner_process_id) : undefined,
  };
}

export async function sendChat(
  client: BackendClient,
  project: string,
  text: string,
  startTime: string,
  pendingDecision: PendingDecision | null,
  cb: ChatSendCallbacks,
  options: {manualDelegation?: boolean; projectId?: string; workspace?: string} = {},
): Promise<void> {
  // --mock mode: simulate a token stream so the live dashboard is demoed.
  if ((client as unknown) instanceof MockMcpClient) {
    await simulateMockStream({ onStream: cb.onStream, onDone: cb.onDone });
    return;
  }

  if (client instanceof NativeHostClient && client.ready) {
      let state = initStreamState();
      let rootProcessId = "";
      await client.sendTaskStreaming(
        {
          project,
          message: text,
          expected_project_id: options.projectId || undefined,
          expected_workspace: options.workspace || undefined,
          decision: pendingDecision || undefined,
          ...(options.manualDelegation ? {manual_delegation: true} : {}),
        },
        {
          onAcknowledged: (ack) => {
            const processId = String(ack?.process_id || "");
            if (processId) rootProcessId = processId;
            // "accepted" deliberately precedes daemon admission. Preserve the
            // native request identity so Esc can cancel recovery/admission even
            // before a process id exists.
            cb.onAcknowledged?.({
              requestId: String(ack?.request_id || ""),
              processId,
              taskId: String(ack?.task_id || ""),
              stage: String(ack?.stage || ""),
            });
            const stage = String(ack?.stage || "");
            const label = stage === "accepted"
              ? "Starting runtime and restoring the session…"
              : stage === "admitted" ? "Runtime ready; starting work…" : "";
            if (label) {
              const activity = state.activity.filter((item) => !(
                item.kind === "progress"
                && (item.text === "Starting runtime and restoring the session…"
                  || item.text === "Runtime ready; starting work…")
              ));
              state = {
                ...state,
                activity: [...activity, {kind: "progress", text: label, visibility: "public"}],
              };
              cb.onStream({
                text: state.text,
                reasoning: state.reasoning,
                tools: state.tools,
                notices: state.notices,
                activity: state.activity,
                timestamp: startTime,
                visibility: state.visibility,
                providerStep: state.providerStep,
                provider_usage: state.providerUsage,
              });
            }
          },
          onChunk: (event) => {
            publishProcessStreamEvent(project, event, startTime);
            const eventProcessId = String(event.process_id || "");
            const declaredRoot = String(
              "root_process_id" in event ? event.root_process_id || "" : "",
            );
            const parentProcessId = String(
              "parent_process_id" in event ? event.parent_process_id || "" : "",
            );
            if (!rootProcessId) {
              rootProcessId = declaredRoot || (!parentProcessId ? eventProcessId : "");
            }
            // A sees child lifecycle/tool evidence (including diffs), while
            // each B retains its own full reasoning/text stream.
            state = reduceSupervisorEvent(state, event, rootProcessId);
            cb.onStream({
              text: state.text,
              reasoning: state.reasoning,
              tools: state.tools,
              notices: state.notices,
              activity: state.activity,
              timestamp: startTime,
              visibility: state.visibility,
              providerStep: state.providerStep,
              provider_usage: state.providerUsage,
            });
          },
          onComplete: (result: any) => {
            const outcome = result?.outcome;
            if (!outcome) {
              cb.onError("agent_complete missing TaskOutcome");
              return;
            }
            if (outcome.status === "completed") {
              cb.onDone(
                outcome.response || state.text || "(no reply)",
                finalizeTools(state.tools), state.notices, null,
                completedRound(outcome, state),
              );
              return;
            }
            if (outcome.status === "awaiting_user") {
              const decision = pendingDecisionFromOutcome(outcome);
              if (!decision) {
                cb.onError("awaiting_user outcome omitted structured decision identity");
                return;
              }
              cb.onDone(
                outcome.response || "任务需要你的选择；请回复选项或补充条件。",
                finalizeTools(state.tools),
                state.notices,
                decision,
                completedRound(outcome, state),
              );
              return;
            }
            if (outcome.status === "degraded" && outcome.response) {
              cb.onDone(
                `[Error: ${outcomeError(outcome)}]\n\n${outcome.response}`,
                finalizeTools(state.tools), state.notices, null,
                completedRound(outcome, state),
              );
              return;
            }
            cb.onError(outcomeError(outcome), completedRound(outcome, state));
          },
          onError: (err: Error) => cb.onError(err.message),
        },
      );
      return;
  }

  // Non-streaming BackendClient path (primarily test doubles).
  try {
    const result: any = await agentChat(client, project, text);
    if (!["completed", "degraded", "awaiting_user"].includes(result?.status)) {
      cb.onError(outcomeError(result));
      return;
    }
    const responseText = result?.status === "degraded"
      ? `[Error: ${outcomeError(result)}]\n\n${result?.response || ""}`
      : result?.response || "(no reply)";
    const processId = result?.process_id || "";

    let tools: ToolCallCard[] | undefined;
    try {
      const loopData: any = await loopStatus(client, project);
      const events: ToolEvent[] = (loopData?.recent_tool_executed || []) as ToolEvent[];
      tools = events
        .filter((e) => {
          if (processId && e.process_id !== processId) return false;
          if (e.timestamp < startTime) return false;
          return true;
        })
        .map((e) => ({
          tool_name: e.tool_name,
          target: "",
          allowed: e.allowed,
          status_label: e.allowed ? "OK" : "DENIED",
          duration_ms: e.duration_ms,
          timestamp: e.timestamp,
          blocked_reason: e.blocked_reason,
          diff: e.diff,
          state: e.allowed ? ("completed" as const) : ("error" as const),
        }));
      if (tools.length === 0) tools = undefined;
    } catch {
      // loop_status unavailable — set message without tools.
    }

    cb.onDone(
      responseText,
      tools,
      undefined,
      pendingDecisionFromOutcome(result),
      completedRound(result),
    );
  } catch {
    cb.onError("call failed");
  }
}
