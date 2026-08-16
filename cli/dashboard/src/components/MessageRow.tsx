// src/components/MessageRow.tsx — shared persisted-message renderer.
// Role-based layering: user = full-width gray block + `❯` marker,
// assistant = gutter `●`, system = dim. Wrapped lines align under the marker.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ChatMessage } from "../types.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import { colors, wrap } from "../theme/index.js";

type Props = {
  msg: ChatMessage;
  contentWidth: number;
};

export function MessageRow({ msg, contentWidth }: Props) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  const prefix = isUser ? "❯ " : isSystem ? "sys " : "● ";
  const prefixW = prefix.length;
  const lines = wrap(msg.content, Math.max(10, contentWidth - prefixW));
  // Drop trailing empty lines (from a trailing newline) so a wrapped message
  // never renders a blank gutter-only line at the end.
  while (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const indent = " ".repeat(prefixW);
  const prefixColor = isUser ? colors.chat.userMarker : isSystem ? undefined : colors.chat.gutter;

  return (
    <Box flexDirection="column" width="100%"
      backgroundColor={isUser ? colors.chat.userBg : undefined}
      paddingLeft={isUser ? 1 : 0} paddingRight={isUser ? 1 : 0}>
      {lines.map((line, i) => (
        <Box key={i} flexDirection="row">
          <Text color={prefixColor} dimColor={isSystem}>{i === 0 ? prefix : indent}</Text>
          <Text dimColor={isSystem}>{line}</Text>
          {i === 0 && msg.pending ? (
            <Text dimColor color={colors.warning}> pending</Text>
          ) : null}
        </Box>
      ))}
      {msg.tools && msg.tools.length > 0 ? (
        <Box flexDirection="column" marginTop={1}>
          {msg.tools.map((tc, ti) => (
            <Box key={ti}>
              <ToolCallDisplay tool={tc} />
            </Box>
          ))}
        </Box>
      ) : null}
    </Box>
  );
}
