// src/components/MessageRow.tsx — shared persisted-message renderer.
// Role-based layering: user = full-width gray block + `❯` marker,
// assistant = gutter `●`, system = dim. Wrapped lines align under the marker.
import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ChatMessage, StreamingRow } from "../types.js";
import type { BackendClient } from "../backend/client.js";
import { roundLabel } from "../chat/roundTrajectory.js";
import { AnimatedReveal } from "./AnimatedReveal.js";
import { RoundDetails } from "./RoundDetails.js";
import { colors, formatDuration } from "../theme/index.js";
import { MarkdownText } from "./MarkdownText.js";

type Props = {
  msg: ChatMessage;
  contentWidth: number;
  verbose?: boolean;
  client?: BackendClient;
  project?: string;
  includeDescendants?: boolean;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  streamSnapshot?: StreamingRow;
};

export function MessageRow({
  msg,
  contentWidth,
  verbose = false,
  client,
  project,
  includeDescendants = false,
  expanded: controlledExpanded,
  onExpandedChange,
  streamSnapshot,
}: Props) {
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlledExpanded ?? localExpanded;
  const setExpanded = (value: boolean) => {
    if (controlledExpanded === undefined) setLocalExpanded(value);
    onExpandedChange?.(value);
  };
  const label = roundLabel(msg);
  const [settling, setSettling] = useState(Boolean(streamSnapshot && label));
  useEffect(() => {
    if (!settling) return;
    const timer = setTimeout(() => setSettling(false), 25);
    return () => clearTimeout(timer);
  }, [settling]);
  const detailMessage = streamSnapshot
    ? {
        ...msg,
        activity: msg.activity || streamSnapshot.activity?.filter(item => item.kind !== "draft"),
        tools: msg.tools || streamSnapshot.tools,
        notices: msg.notices || streamSnapshot.notices,
        reasoning: msg.reasoning || streamSnapshot.reasoning,
        provider_usage: msg.provider_usage || streamSnapshot.provider_usage,
      }
    : msg;
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  const prefix = isUser ? "❯ " : isSystem ? "sys " : "● ";
  const prefixW = prefix.length;
  const prefixColor = isUser
    ? colors.chat.userMarker
    : isSystem
      ? undefined
      : colors.chat.gutter;

  return (
    <Box
      flexDirection="column"
      width="100%"
      backgroundColor={isUser ? colors.chat.userBg : undefined}
      paddingLeft={isUser ? 1 : 0}
      paddingRight={isUser ? 1 : 0}
    >
      {label ? (
        <Box paddingLeft={2} onClick={() => setExpanded(!expanded)}>
          <Text dimColor>
            {expanded || settling ? "▾" : "▸"} {label} ·{" "}
            {formatDuration(msg.duration_ms || 0)}
          </Text>
        </Box>
      ) : null}
      {label ? (
        <AnimatedReveal expanded={expanded || settling}>
          <RoundDetails
            message={detailMessage}
            client={client}
            project={project}
            verbose={verbose}
            width={contentWidth}
            includeDescendants={includeDescendants}
            readTrace={expanded}
          />
        </AnimatedReveal>
      ) : null}
      <Box flexDirection="row">
        <Text color={prefixColor} dimColor={isSystem}>
          {prefix}
        </Text>
        <Box flexDirection="column" flexGrow={1}>
          <MarkdownText
            content={msg.content}
            width={Math.max(10, contentWidth - prefixW)}
            dimColor={isSystem}
          />
        </Box>
        {msg.pending ? (
          <Text dimColor color={colors.warning}>
            {" "}
            pending
          </Text>
        ) : null}
      </Box>
    </Box>
  );
}
