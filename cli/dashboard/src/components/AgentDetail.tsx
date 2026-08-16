// src/components/AgentDetail.tsx — v4 blueprint: B Agent chat conversation
import React, { memo, useEffect, useRef } from "react";
import { Box, Text, ScrollBox } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import type { ProcessInfo, ToolEvent } from "../hooks/useLoopData.js";
import { useLoopData } from "../hooks/useLoopData.js";
import type { ChatMessage, StreamingRow, ChatScrollHandle } from "../types.js";
import { useChat } from "../hooks/useChat.js";
import { MessageRow } from "./MessageRow.js";
import { StreamingMessage } from "./StreamingMessage.js";
import { colors, usePanelSize } from "../theme/index.js";

// ── AgentDetail component ──────────────────────────────────

type Props = {
  process: ProcessInfo;
  toolEvents: ToolEvent[];
  messages: ChatMessage[];
  streaming: StreamingRow | null;
  cols: number;
  rows: number;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
};

export const AgentDetail = memo(function AgentDetail({
  process, toolEvents, messages, streaming, cols, rows, scrollChatRef,
}: Props) {
  const { w: width } = usePanelSize({ minWidth: 50 });
  const contentWidth = width - 4;
  const scrollRef = useRef<ScrollBoxHandle>(null);

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
        <Text bold>B Agent — {process.role || "agent"}</Text>
        <Text dimColor>  {process.steps_used}/{process.max_steps} steps</Text>
      </Box>

      {/* Chat conversation */}
      <ScrollBox ref={scrollRef} stickyScroll flexDirection="column" flexGrow={1}>
        {visibleMessages.length === 0 ? (
          <Box paddingTop={1}>
            <Text dimColor>No conversation recorded</Text>
          </Box>
        ) : (
          visibleMessages.map((msg, idx) => {
            const msgKey = `${msg.timestamp}-${msg.role}-${msg.content.slice(0, 20)}`;
            return (
              <Box key={msgKey} marginTop={idx > 0 ? 1 : 0} width="100%">
                <MessageRow msg={msg} contentWidth={contentWidth} />
              </Box>
            );
          })
        )}

        {/* Transient streaming row — separate from the persisted list */}
        {streaming ? (
          <StreamingMessage streaming={streaming} contentWidth={contentWidth} />
        ) : null}
      </ScrollBox>
    </Box>
  );
});

// ── AgentDetailScene — data-fetching wrapper ────────────────

type SceneProps = {
  client: McpClient;
  activeProject: string | null;
  activeAgentId: string | null;
  cols: number;
  rows: number;
  sendChatRef?: React.MutableRefObject<(text: string) => void>;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
};

export const AgentDetailScene = memo(function AgentDetailScene({
  client, activeProject, activeAgentId, cols, rows, sendChatRef, scrollChatRef,
}: SceneProps) {
  // Poll live (2s) so the B-side conversation is not frozen on a single fetch.
  const loop = useLoopData(client, activeProject, 2);
  const conv = loop.agentConversations?.[activeAgentId ?? ""] ?? null;
  const { messages, streaming, send } = useChat(client, activeProject || "", conv);

  useEffect(() => {
    if (sendChatRef) {
      sendChatRef.current = (text: string) => { send(text); };
    }
  }, [send, sendChatRef]);

  if (loop.loading) {
    return (
      <Box paddingLeft={1}>
        <Text dimColor>Loading agent data...</Text>
      </Box>
    );
  }

  const process = activeAgentId ? loop.processes[activeAgentId] : null;
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
      process={process}
      toolEvents={loop.toolEvents}
      messages={messages}
      streaming={streaming}
      cols={cols}
      rows={rows}
      scrollChatRef={scrollChatRef}
    />
  );
});
