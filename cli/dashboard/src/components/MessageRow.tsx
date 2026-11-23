// src/components/MessageRow.tsx — shared persisted-message renderer.
// Role-based layering: user = full-width gray block + `❯` marker,
// assistant = gutter `●`, system = dim. Wrapped lines align under the marker.
import React, { useMemo, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ChatMessage, StreamingRow } from "../types.js";
import type { BackendClient } from "../backend/client.js";
import { roundLabel } from "../chat/roundTrajectory.js";
import {Disclosure} from "./Disclosure.js";
import {timelineFromTrace,withProcessMessages} from "../chat/turnTimeline.js";
import {useTraceHistory} from "../hooks/useTraceHistory.js";
import { RoundDetails } from "./RoundDetails.js";
import { colors, formatDuration } from "../theme/index.js";
import { MarkdownText } from "./MarkdownText.js";
import {useCopy} from "../ui/PageFrame.js";
import {geometry} from "../theme/tokens.js";
import {expandedValue, type ExpandedChange} from "../hooks/useDisclosureState.js";

type Props = {
  msg: ChatMessage;
  contentWidth: number;
  verbose?: boolean;
  client?: BackendClient;
  project?: string;
  includeDescendants?: boolean;
  expanded?: boolean;
  onExpandedChange?: (expanded: ExpandedChange) => void;
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
  const t=useCopy();
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlledExpanded ?? localExpanded;
  const setExpanded = (value: ExpandedChange) => {
    if (expandedValue(expanded, value) && !expanded && history.unavailable) history.retry();
    if (controlledExpanded === undefined) setLocalExpanded(value);
    onExpandedChange?.(value);
  };
  const label = roundLabel(msg, t);
  const processOnly = msg.role === "assistant" && msg.kind === "progress";
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
  // The completed/failed capsule is the sole owner of every process record.
  // A folded round performs no history/body reads, regardless of verbose.
  const history = useTraceHistory(client, project, msg.trace_id, msg.process_id || "",
    includeDescendants, msg.status || "", Boolean(label && expanded));
  const timeline = useMemo(() => {
    const index = history.index;
    const fallback = {
      activity:detailMessage.activity?.length ? detailMessage.activity : [
        ...(processOnly ? [{kind:"progress" as const,text:msg.content,visibility:msg.visibility === "verbose" ? "verbose" as const : "public" as const,
          record_id:msg.message_id,process_id:msg.process_id,update_id:msg.update_id,progress_status:msg.progress_status}] : []),
        ...(detailMessage.reasoning ? [{kind:"reasoning" as const,text:detailMessage.reasoning}] : []),
        ...(detailMessage.tools || []).map((_,toolIndex) => ({kind:"tool" as const,toolIndex})),
        ...(detailMessage.notices || []).map((_,noticeIndex) => ({kind:"notice" as const,noticeIndex})),
      ],
      tools:detailMessage.tools || [],notices:detailMessage.notices || [],providerUsage:detailMessage.provider_usage,
    };
    const model = index?.events.length ? timelineFromTrace(index.events, msg.process_id || "", {
      status:index.complete ? msg.status : "running",timestamp:msg.timestamp,
    }) : fallback;
    return withProcessMessages(model,msg.process_messages,msg.turn_id);
  }, [history.index, detailMessage, msg, processOnly]);
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
      minWidth={0}
      backgroundColor={isUser ? colors.chat.userBg : undefined}
      paddingLeft={isUser ? 1 : 0}
      paddingRight={isUser ? 1 : 0}
    >
      {label ? (
        <Disclosure headerInset={geometry.record.indent} contentInset={geometry.record.indent}
          label={label + (typeof msg.duration_ms === "number" ? ` · ${formatDuration(msg.duration_ms)}` : "")}
          expanded={expanded} onToggle={() => setExpanded(current => !current)}>
          <RoundDetails
            message={detailMessage}
            client={client}
            project={project}
            verbose={verbose}
            width={Math.max(1,contentWidth-geometry.record.indent)}
            history={history}
            timeline={timeline}
          />
        </Disclosure>
      ) : null}
      {!processOnly ? <Box flexDirection="row">
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
            {t({en:"pending",zh:"等待中"})}
          </Text>
        ) : null}
      </Box> : null}
    </Box>
  );
}
