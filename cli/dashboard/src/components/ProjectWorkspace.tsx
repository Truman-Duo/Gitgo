// src/components/ProjectWorkspace.tsx — Scene 2: full-width chat (v4 blueprint aligned)
import React, { memo, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import type { ChatScrollHandle, PendingDecision } from "../types.js";
import type { LoopData } from "../hooks/useLoopData.js";
import { useChat } from "../hooks/useChat.js";
import { ChatPanel } from "./ChatPanel.js";
import { colors } from "../theme/index.js";

type Props = {
  project: string;
  projectId?: string;
  workspace?: string;
  client: BackendClient;
  loopData: LoopData;
  cols: number;
  rows: number;
  onBack: () => void;
  onEnterAgent: (processId: string) => void;
  refreshKey: number;
  sendChatRef?: React.MutableRefObject<(text: string) => void>;
  sendChatReadyRef?: React.MutableRefObject<boolean>;
  manualCreateRef?: React.MutableRefObject<(text: string) => boolean>;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
  decisionSelection: number;
  decisionComposing: boolean;
  decisionSubmitting: boolean;
  onDecisionChange: (decision: PendingDecision | null) => void;
  onSendSettled: () => void;
  onBusyChange: (busy: boolean) => void;
  onActiveProcessChange: (processId: string) => void;
  onActiveRequestChange: (requestId: string) => void;
  verbose?: boolean;
  visible?: boolean;
};

export const ProjectWorkspace = memo(function ProjectWorkspace({
  project, projectId, workspace, client, loopData, cols, sendChatRef, sendChatReadyRef, manualCreateRef, scrollChatRef,
  decisionSelection, decisionComposing, decisionSubmitting,
  onDecisionChange, onSendSettled, onBusyChange, onActiveProcessChange, onActiveRequestChange,
  verbose = false,
  visible = true,
}: Props) {
  const { loading, error, mainConversation, processes } = loopData;
  const rootProcess = Object.values(processes)
    .filter((process) => process.parent_id === null && process.actor_kind === "supervisor")
    .sort((left, right) => {
      const leftActive = ["running", "waiting", "awaiting_user"].includes(left.status) ? 1 : 0;
      const rightActive = ["running", "waiting", "awaiting_user"].includes(right.status) ? 1 : 0;
      return rightActive - leftActive || right.created_at.localeCompare(left.created_at);
    })[0];
  const routedDecision = rootProcess?.pending_decision || loopData.pendingQuestions.find(question =>
    !rootProcess?.process_id || question.owner_process_id === rootProcess.process_id
  ) || null;
  const { messages, streaming, pendingDecision, activeProcessId, activeRequestId, send, submitManual } = useChat(
    client,
    project,
    {projectId, workspace},
    mainConversation,
    routedDecision,
  );

  useEffect(() => {
    if (manualCreateRef) manualCreateRef.current = submitManual;
    return () => { if (manualCreateRef) manualCreateRef.current = () => false; };
  }, [manualCreateRef, submitManual]);

  useEffect(() => {
    if (sendChatRef && visible) {
      sendChatRef.current = (text: string) => { void send(text).finally(onSendSettled); };
      if (sendChatReadyRef) sendChatReadyRef.current = true;
    }
    return () => {
      if (sendChatReadyRef) sendChatReadyRef.current = false;
    };
  }, [send, sendChatRef, sendChatReadyRef, onSendSettled, visible]);

  useEffect(() => {
    if (!visible) return;
    onDecisionChange(pendingDecision);
    return () => onDecisionChange(null);
  }, [pendingDecision, onDecisionChange, visible]);

  useEffect(() => {
    onBusyChange(Boolean(streaming));
    return () => onBusyChange(false);
  }, [streaming, onBusyChange]);

  useEffect(() => {
    onActiveProcessChange(activeProcessId);
    return () => onActiveProcessChange("");
  }, [activeProcessId, onActiveProcessChange]);

  useEffect(() => {
    onActiveRequestChange(activeRequestId);
    return () => onActiveRequestChange("");
  }, [activeRequestId, onActiveRequestChange]);

  return (
    <Box flexDirection="column" width={cols} paddingTop={1} flexGrow={1}>
      {loading ? (
        <Box paddingLeft={1}><Text dimColor>Loading loop data...</Text></Box>
      ) : null}
      {error ? (
        <Box paddingLeft={1}><Text color={colors.danger}>Runtime status unavailable: {error}</Text></Box>
      ) : null}
      <ChatPanel
        client={client}
        messages={messages}
        streaming={streaming}
        pendingDecision={pendingDecision}
        decisionSelection={decisionSelection}
        decisionComposing={decisionComposing}
        decisionSubmitting={decisionSubmitting}
        cols={cols}
        project={project}
        scrollChatRef={scrollChatRef}
        verbose={verbose}
        inputActive={visible}
      />
    </Box>
  );
});
