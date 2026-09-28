// src/components/ChatPanel.tsx — Scene 2 left pane: message list + tool cards
import React, { memo, useEffect, useRef } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { ChatMessage, PendingDecision, StreamingRow, ChatScrollHandle } from "../types.js";
import { usePanelSize } from "../theme/index.js";
import { useScrollInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { VirtualConversation } from "./VirtualConversation.js";

type Props = {
  messages: ChatMessage[];
  streaming: StreamingRow | null;
  cols: number;
  project?: string;
  client?: BackendClient;
  pendingDecision?: PendingDecision | null;
  decisionSelection?: number;
  decisionComposing?: boolean;
  decisionSubmitting?: boolean;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
  verbose?: boolean;
  inputActive?: boolean;
};

export function visibleConversationMessages(messages: ChatMessage[]): ChatMessage[] {
  // Provider/system steering is never public conversation.  A knowledge
  // notice is different: the Host has already reduced the maintenance event
  // to an explicit, privacy-safe user notification, so hiding it would make a
  // successful background harvest indistinguishable from silence.
  return messages.filter((message) =>
    message.role !== "system" || message.kind === "knowledge_notice"
  );
}

export const ChatPanel = memo(function ChatPanel({
  messages,
  streaming,
  cols,
  project,
  client,
  pendingDecision,
  decisionSelection = 0,
  decisionComposing = false,
  decisionSubmitting = false,
  scrollChatRef,
  verbose = false,
  inputActive = true,
}: Props) {
  const { w: terminalWidth } = usePanelSize({ minWidth: 30 });
  const width = Math.max(30, Math.min(terminalWidth, cols));
  const contentWidth = width - 4;
  const scrollRef = useRef<ScrollBoxHandle>(null);
  useScrollInput(scrollRef, inputActive);

  useEffect(() => {
    if (!inputActive) return;
    const handle = scrollRef.current;
    scrollChatRef.current = handle
      ? { scrollBy: (dy: number) => handle.scrollBy(dy), scrollToBottom: () => handle.scrollToBottom() }
      : null;
    return () => { scrollChatRef.current = null; };
  }, [scrollChatRef, inputActive]);

  const allMessages = visibleConversationMessages(messages);

  return (
    <Box flexDirection="column" width={width} paddingLeft={1} flexGrow={1}>
      <Box flexShrink={0}>
        <Text bold>Chat — {project || "Main process"}</Text>
      </Box>

      <VirtualConversation
        scrollRef={scrollRef}
        messages={allMessages}
        streaming={streaming}
        contentWidth={contentWidth}
        client={client}
        project={project}
        pendingDecision={pendingDecision}
        decisionSelection={decisionSelection}
        decisionComposing={decisionComposing}
        decisionSubmitting={decisionSubmitting}
        verbose={verbose}
        includeDescendants
        empty={(
          <Box paddingTop={1} flexDirection="column">
            <Text dimColor>Type a task and press Enter. The main process supervises and delegates when needed.</Text>
            <Text dimColor>Use /runtime for governance data; /processlist opens every subprocess in this project.</Text>
          </Box>
        )}
      />
    </Box>
  );
});
