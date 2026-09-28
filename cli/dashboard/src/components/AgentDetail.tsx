// src/components/AgentDetail.tsx — subprocess chat conversation
import React, { memo, useEffect, useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import type { LoopData, ProcessInfo, ToolEvent } from "../hooks/useLoopData.js";
import type { ChatMessage, StreamingRow, ChatScrollHandle, PendingDecision } from "../types.js";
import { colors, truncate, usePanelSize } from "../theme/index.js";
import { sendRuntimeFeedback } from "../backend/tools.js";
import { publishProcessStreamEvent, useProcessStream } from "../daemon/processStreams.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { useScrollInput } from "../input/runtime.js";
import { agentLabel } from "../daemon/agentLabels.js";
import { sendChat } from "../chat/sendChat.js";
import { VirtualConversation } from "./VirtualConversation.js";

// ── AgentDetail component ──────────────────────────────────

type Props = {
  process: ProcessInfo;
  toolEvents: ToolEvent[];
  messages: ChatMessage[];
  streaming: StreamingRow | null;
  cols: number;
  rows: number;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
  feedbackStatus?: string;
  verbose?: boolean;
  client?: BackendClient;
  project?: string;
  pendingDecision?: PendingDecision | null;
  decisionSelection?: number;
  decisionComposing?: boolean;
  decisionSubmitting?: boolean;
};

export const AgentDetail = memo(function AgentDetail({
  process, toolEvents, messages, streaming, cols, rows, scrollChatRef, feedbackStatus, verbose = false, client, project,
  pendingDecision, decisionSelection = 0, decisionComposing = false, decisionSubmitting = false,
}: Props) {
  const { w: terminalWidth } = usePanelSize({ minWidth: 50 });
  const width = Math.max(50, Math.min(terminalWidth, cols));
  const contentWidth = width - 4;
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef);

  useEffect(() => {
    const handle = scrollRef.current;
    scrollChatRef.current = handle
      ? { scrollBy: (dy: number) => handle.scrollBy(dy), scrollToBottom: () => handle.scrollToBottom() }
      : null;
    return () => { scrollChatRef.current = null; };
  }, [scrollChatRef]);

  const visibleMessages = messages.filter((m) => m.role !== "system");

  return (
    <Box flexDirection="column" paddingLeft={1} width={width} flexGrow={1}>
      {/* Header — name + steps only */}
      <Box flexShrink={0} flexDirection="row">
        <Text bold>Subprocess — {agentLabel(process)}</Text>
        <Text dimColor>  {process.steps_used}/{process.max_steps} steps</Text>
      </Box>
      {process.recovery ? (
        <Box flexShrink={0} flexDirection="column">
          <Text color={process.recovery.resume_forbidden ? colors.danger : colors.warning}>
            Recovery {process.status}
            {process.recovery.requires_manual_verification ? " · manual verification required" : " · explicit resume required"}
          </Text>
          <Text dimColor>
            /runtime recovery {process.recovery.resume_forbidden ? "discard" : process.recovery.requires_manual_verification ? "resume_verified" : "resume"} {process.process_id}
          </Text>
        </Box>
      ) : null}
      {process.worktree ? (
        <Box flexShrink={0} flexDirection="row">
          <Text color={process.worktree.isolated ? colors.success : colors.warning}>
            worktree {process.worktree.isolated ? "isolated" : "shared workspace"}
          </Text>
          {process.worktree.path ? <Text dimColor> · {truncate(process.worktree.path, 70)}</Text> : null}
          {process.worktree.state ? <Text dimColor> · {process.worktree.state}</Text> : null}
          {process.worktree.promoted ? <Text color={colors.success}> · promoted</Text> : null}
          {process.worktree.dirty === true ? <Text color={colors.warning}> · dirty</Text> : null}
        </Box>
      ) : null}
      <Box flexShrink={0} flexDirection="row">
        <Text dimColor>Input is delivered to this subprocess through its safe-boundary mailbox.</Text>
        {feedbackStatus ? <Text color={colors.accent}>  {feedbackStatus}</Text> : null}
      </Box>

      {/* Chat conversation */}
      <VirtualConversation
        scrollRef={scrollRef}
        messages={visibleMessages}
        streaming={streaming}
        contentWidth={contentWidth}
        client={client}
        project={project}
        pendingDecision={pendingDecision}
        decisionSelection={decisionSelection}
        decisionComposing={decisionComposing}
        decisionSubmitting={decisionSubmitting}
        verbose={verbose}
        empty={(
          <Box paddingTop={1}>
            <Text dimColor>No conversation recorded</Text>
          </Box>
        )}
      />
    </Box>
  );
});

// ── AgentDetailScene — data-fetching wrapper ────────────────

type SceneProps = {
  client: BackendClient;
  loopData: LoopData;
  activeProject: string | null;
  activeAgentId: string | null;
  cols: number;
  rows: number;
  sendChatRef?: React.MutableRefObject<(text: string) => void>;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
  verbose?: boolean;
  decisionSelection: number;
  decisionComposing: boolean;
  decisionSubmitting: boolean;
  onDecisionChange: (decision: PendingDecision | null) => void;
  onSendSettled: () => void;
  onContinueAgent?: (processId: string) => void;
};

export const AgentDetailScene = memo(function AgentDetailScene({
  client, loopData, activeProject, activeAgentId, cols, rows, sendChatRef, scrollChatRef, verbose = false,
  decisionSelection, decisionComposing, decisionSubmitting, onDecisionChange, onSendSettled,
  onContinueAgent,
}: SceneProps) {
  const loop = loopData;
  const conv = loop.agentConversations?.[activeAgentId ?? ""] ?? null;
  const processStream = useProcessStream(activeProject, activeAgentId);
  const process = activeAgentId ? loop.processes[activeAgentId] : null;
  const processIsTerminal = [
    "completed", "failed", "cancelled", "timed_out", "killed", "orphaned",
  ].includes(process?.status || "");
  // A terminal event can arrive before the SQLite conversation projection.
  // Keep the last live timeline visible until its durable outcome is present;
  // otherwise the subprocess's final tool/diff rows disappear for one refresh cycle.
  const durableOutcomePresent = Boolean(conv?.some((message) =>
    message.process_id === activeAgentId && message.kind === "outcome" && !message.pending,
  ));
  const [feedbackStatus, setFeedbackStatus] = useState("");
  const decisionBusy = useRef(false);
  const pendingDecision = process?.pending_decision || null;

  useEffect(() => {
    onDecisionChange(pendingDecision);
    return () => onDecisionChange(null);
  }, [pendingDecision, onDecisionChange]);

  useEffect(() => {
    if (sendChatRef) {
      sendChatRef.current = (text: string) => {
        if (!activeProject || !activeAgentId || !text.trim()) return;
        if (pendingDecision) {
          if (decisionBusy.current) return;
          decisionBusy.current = true;
          setFeedbackStatus("submitting decision…");
          void sendChat(client, activeProject, text.trim(), new Date().toISOString(), pendingDecision, {
            onStream: () => undefined,
            onDone: () => {
              setFeedbackStatus("decision delivered");
              void loop.refresh();
            },
            onError: (message) => setFeedbackStatus(`decision failed: ${message}`),
          }).finally(() => {
            decisionBusy.current = false;
            onSendSettled();
          });
          return;
        }
        setFeedbackStatus("sending feedback…");
        const startedAt = new Date().toISOString();
        void sendRuntimeFeedback(client, activeProject, activeAgentId, text.trim(), (event) => {
          const payload = (event?.payload || event) as Record<string, any>;
          if (payload?.process_id) {
            publishProcessStreamEvent(
              activeProject, payload as StreamEvent, startedAt,
            );
          }
          if (payload?.event === "runtime_ack" && payload?.stage === "admitted" && payload?.process_id) {
            setFeedbackStatus("continuation admitted");
            onContinueAgent?.(String(payload.process_id));
          }
        })
          .then((result: any) => {
            setFeedbackStatus("feedback completed");
            if (result?.process_id && result.process_id !== activeAgentId) {
              onContinueAgent?.(String(result.process_id));
            }
            void loop.refresh();
          })
          .catch((error) => setFeedbackStatus(`feedback failed: ${String(error)}`));
      };
    }
    return () => {
      if (sendChatRef) sendChatRef.current = () => {};
    };
  }, [client, activeProject, activeAgentId, sendChatRef, pendingDecision, loop, onSendSettled, onContinueAgent]);

  if (loop.loading) {
    return (
      <Box paddingLeft={1}>
        <Text dimColor>Loading agent data...</Text>
      </Box>
    );
  }

  if (!process) {
    return (
      <Box paddingLeft={1} flexDirection="column">
        <Text color={colors.danger}>Agent not found: {activeAgentId}</Text>
        <Text dimColor>Process may have been killed or completed.</Text>
      </Box>
    );
  }

  return (
    <AgentDetail
      client={client}
      project={activeProject || undefined}
      process={process}
      toolEvents={loop.toolEvents}
      messages={conv ?? []}
      streaming={processIsTerminal && durableOutcomePresent ? null : processStream}
      cols={cols}
      rows={rows}
      scrollChatRef={scrollChatRef}
      feedbackStatus={feedbackStatus}
      verbose={verbose}
      pendingDecision={pendingDecision}
      decisionSelection={decisionSelection}
      decisionComposing={decisionComposing}
      decisionSubmitting={decisionSubmitting}
    />
  );
});
