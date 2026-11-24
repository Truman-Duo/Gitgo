import {useCopy} from "../ui/PageFrame.js";
import {label,processDisplayStatus} from "../ui/runtimeLabels.js";
import {scrollViewport} from "../input/scrollViewport.js";
// src/components/AgentDetail.tsx — subprocess chat conversation
import React, { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
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
  const t=useCopy();
  const { w: terminalWidth } = usePanelSize({ minWidth: 50 });
  const width = Math.max(1, Math.min(terminalWidth, cols));
  const contentWidth = Math.max(1, width - 4);
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef);

  useEffect(() => {
    const handle = scrollRef.current;
    scrollChatRef.current = handle
      ? { scrollBy: (dy: number) => scrollViewport(handle,dy), scrollToBottom: () => handle.scrollToBottom() }
      : null;
    return () => { scrollChatRef.current = null; };
  }, [scrollChatRef]);

  const visibleMessages = messages.filter((m) => m.role !== "system");

  return (
    <Box flexDirection="column" paddingLeft={1} width={width} flexGrow={1}>
      <Box flexShrink={0} minHeight={1}><Text bold wrap="truncate-end">{agentLabel(process)} · {label(processDisplayStatus(process),t)}</Text></Box>
      {process.recovery?<Box flexShrink={0}><Text color={process.recovery.resume_forbidden?colors.danger:colors.warning}>
        {process.recovery.resume_forbidden?t({en:"Recovery blocked; inspect /detail.",zh:"当前禁止恢复，请在 /detail 查看恢复条件。"})
          :process.recovery.requires_manual_verification?t({en:"Manual verification required before resuming.",zh:"恢复前需要人工核验。"})
          :t({en:"Explicit confirmation required before resuming.",zh:"恢复前需要明确确认。"})}
      </Text></Box>:null}
      {process.worktree&&process.worktree.state!=="shared_workspace"&&process.worktree.state!=="isolated"
       ?<Text color={colors.warning}>{t({en:"Worktree: ",zh:"工作树："})}{label(process.worktree.state,t)}</Text>:null}
      {feedbackStatus?<Box flexShrink={0}><Text color={colors.accent}>{feedbackStatus}</Text></Box>:null}

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
            <Text dimColor>{t({en:"No conversation recorded",zh:"暂无对话记录"})}</Text>
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
  decisionView?: PendingDecision | null;
  onDecisionChange: (decision: PendingDecision | null) => void;
  onSendSettled: () => void;
  onContinueAgent?: (processId: string) => void;
};

export const AgentDetailScene = memo(function AgentDetailScene({
  client, loopData, activeProject, activeAgentId, cols, rows, sendChatRef, scrollChatRef, verbose = false,
  decisionSelection, decisionComposing, decisionSubmitting, onDecisionChange, onSendSettled,
  decisionView,
  onContinueAgent,
}: SceneProps) {
  const t=useCopy();
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
  const requestSequence = useRef(0);
  const viewOwner = useRef({project: activeProject, processId: activeAgentId, client, active: false});
  const admittedNavigation = useRef<{owner: typeof viewOwner.current; processId: string} | null>(null);
  useLayoutEffect(() => {
    const navigation = admittedNavigation.current;
    const previous = viewOwner.current;
    const ownContinuation = navigation?.owner === previous && navigation.processId === activeAgentId
      && previous.project === activeProject && previous.client === client;
    const owner = ownContinuation ? previous : {project: activeProject, processId: activeAgentId, client, active: true};
    owner.active = true; owner.processId = activeAgentId;
    viewOwner.current = owner;
    admittedNavigation.current = null;
    if (!ownContinuation) {
      ++requestSequence.current;
      decisionBusy.current = false;
      setFeedbackStatus("");
    }
    return () => {owner.active = false;};
  }, [activeProject, activeAgentId, client]);
  const pendingDecision = process?.pending_decision || null;

  useEffect(() => {
    onDecisionChange(pendingDecision);
    return () => onDecisionChange(null);
  }, [pendingDecision, onDecisionChange]);

  useEffect(() => {
    if (sendChatRef) {
      sendChatRef.current = (text: string) => {
        if (!activeProject || !activeAgentId || !text.trim()) return;
        if (pendingDecision && decisionBusy.current) return;
        const owner = viewOwner.current;
        if (!owner.active || owner.project !== activeProject || owner.processId !== activeAgentId || owner.client !== client) return;
        const sequence = ++requestSequence.current;
        let settled = false;
        const isCurrent = () => !settled && owner.active && viewOwner.current === owner && requestSequence.current === sequence;
        const continueCurrent = (processId: string) => {
          if (!isCurrent() || !onContinueAgent || owner.processId === processId
              || (admittedNavigation.current?.owner === owner && admittedNavigation.current.processId === processId)) return;
          // The admission ack may itself move this view to its successor. Keep
          // this request's eventual success/error visible there, but invalidate
          // it on unrelated process/project navigation or scene unmount.
          admittedNavigation.current = {owner, processId};
          onContinueAgent(processId);
        };
        if (pendingDecision) {
          decisionBusy.current = true;
          setFeedbackStatus(t({en:"submitting decision…",zh:"正在提交答复…"}));
          void sendChat(client, activeProject, text.trim(), new Date().toISOString(), pendingDecision, {
            onStream: () => undefined,
            onDone: () => {
              if (!isCurrent()) return;
              setFeedbackStatus(t({en:"decision delivered",zh:"答复已送达"}));
              void loop.refresh();
            },
            onError: (message) => {if (isCurrent()) setFeedbackStatus(t({en:`decision failed: ${message}`,zh:`答复提交失败：${message}`}));},
          }).finally(() => {
            if (!isCurrent()) return;
            decisionBusy.current = false;
            onSendSettled();
            settled = true;
          });
          return;
        }
        setFeedbackStatus(t({en:"sending feedback…",zh:"正在发送补充…"}));
        const startedAt = new Date().toISOString();
        void sendRuntimeFeedback(client, activeProject, activeAgentId, text.trim(), (event) => {
          const payload = (event?.payload || event) as Record<string, any>;
          if (payload?.process_id) {
            publishProcessStreamEvent(
              activeProject, payload as StreamEvent, startedAt,
            );
          }
          if (isCurrent() && payload?.event === "runtime_ack" && payload?.stage === "admitted" && payload?.process_id) {
            setFeedbackStatus(t({en:"continuation admitted",zh:"后续任务已接收"}));
            continueCurrent(String(payload.process_id));
          }
        })
          .then((result: any) => {
            if (!isCurrent()) return;
            const outcome = result?.outcome;
            const status = outcome?.status || result?.status || "unknown";
            setFeedbackStatus(status === "accepted" ? t({en:"feedback queued",zh:"补充已排队"})
              : status === "completed" ? t({en:"subprocess completed",zh:"子进程已完成"})
              : t({en:"Subprocess: ",zh:"子进程："})+label(status,t)+(outcome?.error?.message?": "+outcome.error.message:""));
            if (result?.process_id && result.process_id !== activeAgentId) {
              continueCurrent(String(result.process_id));
            }
            void loop.refresh();
            settled = true;
          })
          .catch((error) => {
            if (isCurrent()) setFeedbackStatus(t({en:`feedback failed: ${String(error)}`,zh:`补充发送失败：${String(error)}`}));
            settled = true;
          });
      };
    }
    return () => {
      if (sendChatRef) sendChatRef.current = () => {};
    };
  }, [client, activeProject, activeAgentId, sendChatRef, pendingDecision, loop, onSendSettled, onContinueAgent,t]);

  if (loop.loading) {
    return (
      <Box paddingLeft={1}>
        <Text dimColor>{t({en:"Loading subprocess data…",zh:"正在读取子进程数据…"})}</Text>
      </Box>
    );
  }

  if (!process) {
    return (
      <Box paddingLeft={1} flexDirection="column">
        <Text color={colors.danger}>{t({en:"Subprocess not found: ",zh:"未找到子进程："})}{activeAgentId}</Text>
        <Text dimColor>{t({en:"The process may have been removed or become unavailable.",zh:"该进程可能已被移除或暂时不可用。"})}</Text>
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
      pendingDecision={decisionView?.decision_id === pendingDecision?.decision_id ? decisionView : pendingDecision}
      decisionSelection={decisionSelection}
      decisionComposing={decisionComposing}
      decisionSubmitting={decisionSubmitting}
    />
  );
});
