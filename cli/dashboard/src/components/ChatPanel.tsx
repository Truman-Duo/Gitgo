// src/components/ChatPanel.tsx — Scene 2 left pane: message list + tool cards
import React, { memo, useEffect, useRef } from "react";
import { Box, Text, ScrollBox } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { ChatMessage, StreamingRow, ChatScrollHandle } from "../types.js";
import { MessageRow } from "./MessageRow.js";
import { StreamingMessage } from "./StreamingMessage.js";
import { usePanelSize } from "../theme/index.js";

type Props = {
  messages: ChatMessage[];
  streaming: StreamingRow | null;
  cols: number;
  project?: string;
  scrollChatRef: React.MutableRefObject<ChatScrollHandle | null>;
};

export const ChatPanel = memo(function ChatPanel({
  messages,
  streaming,
  cols: _cols,
  project,
  scrollChatRef,
}: Props) {
  const { w: width } = usePanelSize({ minWidth: 30 });
  const contentWidth = width - 4;
  const scrollRef = useRef<ScrollBoxHandle>(null);

  useEffect(() => {
    const handle = scrollRef.current;
    scrollChatRef.current = handle
      ? { scrollBy: (dy: number) => handle.scrollBy(dy), scrollToBottom: () => handle.scrollToBottom() }
      : null;
    return () => { scrollChatRef.current = null; };
  }, [scrollChatRef]);

  const allMessages = messages.filter((m) => m.role !== "system");

  return (
    <Box flexDirection="column" width={width} paddingLeft={1} flexGrow={1}>
      <Box flexShrink={0}>
        <Text bold>Chat — {project || "A Agent"}</Text>
      </Box>

      <ScrollBox ref={scrollRef} stickyScroll flexDirection="column" flexGrow={1}>
        {allMessages.length === 0 ? (
          <Box paddingTop={1}>
            <Text dimColor>Type a message to chat with A Agent...</Text>
          </Box>
        ) : (
          <Box flexDirection="column">
            {allMessages.map((msg, idx) => {
              const msgKey = `${msg.timestamp}-${msg.role}-${msg.content.slice(0, 20)}`;
              return (
                <Box key={msgKey} marginTop={idx > 0 ? 1 : 0} width="100%">
                  <MessageRow msg={msg} contentWidth={contentWidth} />
                </Box>
              );
            })}
          </Box>
        )}

        {/* Transient streaming row — separate from the persisted list */}
        {streaming ? (
          <StreamingMessage streaming={streaming} contentWidth={contentWidth} />
        ) : null}
      </ScrollBox>
    </Box>
  );
});
