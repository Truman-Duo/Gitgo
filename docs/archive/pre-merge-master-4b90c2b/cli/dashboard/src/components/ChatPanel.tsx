// src/components/ChatPanel.tsx — Scene 2 left pane: message list + ScrollBox
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
  timestamp: string;
  pending?: boolean; // unsent bubble pattern (DeepSeek)
};

type Props = {
  messages: ChatMessage[];
  governanceBrief: string | null;
  cols: number;
};

export const ChatPanel = memo(function ChatPanel({
  messages,
  governanceBrief,
  cols,
}: Props) {
  const width = Math.max(30, cols);
  const contentWidth = width - 4;

  const wrap = (text: string, maxW: number) => {
    if (!text) return [""];
    const lines: string[] = [];
    for (const para of text.split("\n")) {
      if (!para) { lines.push(""); continue; }
      let remaining = para;
      while (remaining.length > maxW) {
        lines.push(remaining.slice(0, maxW));
        remaining = remaining.slice(maxW);
      }
      if (remaining) lines.push(remaining);
    }
    return lines;
  };

  const allMessages: ChatMessage[] = [];
  if (governanceBrief && messages.length === 0) {
    allMessages.push({
      role: "system",
      content: governanceBrief,
      timestamp: new Date().toISOString(),
    });
  }
  allMessages.push(...messages);

  return (
    <Box flexDirection="column" width={width} paddingLeft={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">Chat with A</Text>
      </Box>

      <Box flexDirection="column" flexGrow={1}>
        {allMessages.length === 0 ? (
          <Box paddingTop={1}>
            <Text dimColor>输入消息与 A 级 Agent 对话...</Text>
          </Box>
        ) : (
          allMessages.map((msg, i) => {
            const lines = wrap(msg.content, contentWidth);
            const roleColor =
              msg.role === "system"
                ? "gray"
                : msg.role === "user"
                  ? "cyan"
                  : "green";
            const roleLabel =
              msg.role === "system"
                ? "── System ──"
                : msg.role === "user"
                  ? "You"
                  : "A";
            return (
              <Box key={i} flexDirection="column" marginBottom={1}>
                <Box flexDirection="row">
                  <Text color={roleColor} bold>
                    {roleLabel}
                  </Text>
                  {msg.pending ? (
                    <Text dimColor color="yellow"> (pending)</Text>
                  ) : null}
                </Box>
                {lines.map((line, j) => (
                  <Text key={j} dimColor={msg.role === "system"}>
                    {line}
                  </Text>
                ))}
              </Box>
            );
          })
        )}
      </Box>
    </Box>
  );
});
