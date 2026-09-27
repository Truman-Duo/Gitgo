import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Box, ScrollBox, useVirtualScroll } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import type { ChatMessage, PendingDecision, StreamingRow } from "../types.js";
import { DecisionPanel } from "./DecisionPanel.js";
import { MessageRow } from "./MessageRow.js";
import { StreamingMessage } from "./StreamingMessage.js";

type ConversationEntry =
  | { kind: "message"; key: string; message: ChatMessage; messageIndex: number }
  | { kind: "stream"; key: string; streaming: StreamingRow }
  | { kind: "decision"; key: string; decision: PendingDecision };

type Props = {
  messages: ChatMessage[];
  streaming: StreamingRow | null;
  contentWidth: number;
  scrollRef: React.RefObject<ScrollBoxHandle | null>;
  client?: BackendClient;
  project?: string;
  pendingDecision?: PendingDecision | null;
  decisionSelection?: number;
  decisionComposing?: boolean;
  decisionSubmitting?: boolean;
  verbose?: boolean;
  includeDescendants?: boolean;
  empty?: React.ReactNode;
};

function messageKey(message: ChatMessage, index: number): string {
  return message.message_id || message.id ||
    `${message.timestamp}-${message.role}-${message.content.slice(0, 32)}-${index}`;
}

function estimatedRows(entry: ConversationEntry, width: number): number {
  const usable = Math.max(12, width - 4);
  if (entry.kind === "decision") {
    return Math.max(5, 4 + entry.decision.options.length * 2);
  }
  if (entry.kind === "stream") {
    const text = `${entry.streaming.reasoning || ""}\n${entry.streaming.text || ""}`;
    return Math.max(4, 3 + Math.ceil(text.length / usable) + entry.streaming.tools.length * 2);
  }
  const message = entry.message;
  const logicalLines = String(message.content || "").split("\n");
  const wrapped = logicalLines.reduce(
    (rows, line) => rows + Math.max(1, Math.ceil(line.length / usable)),
    0,
  );
  return Math.max(2, wrapped + (message.duration_ms ? 1 : 0) + 1);
}

/** Shared A/B conversation viewport with variable-height message virtualization. */
export function VirtualConversation({
  messages,
  streaming,
  contentWidth,
  scrollRef,
  client,
  project,
  pendingDecision,
  decisionSelection = 0,
  decisionComposing = false,
  decisionSubmitting = false,
  verbose = false,
  includeDescendants = false,
  empty = null,
}: Props) {
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const entries = useMemo<ConversationEntry[]>(() => {
    const result: ConversationEntry[] = messages.map((message, index) => ({
      kind: "message",
      key: `message:${messageKey(message, index)}`,
      message,
      messageIndex: index,
    }));
    if (streaming) {
      result.push({ kind: "stream", key: `stream:${streaming.timestamp}`, streaming });
    }
    if (pendingDecision && !messages.some(
      message => message.decision?.decision_id === pendingDecision.decision_id,
    )) {
      result.push({
        kind: "decision",
        key: `decision:${pendingDecision.decision_id}`,
        decision: pendingDecision,
      });
    }
    return result;
  }, [messages, streaming, pendingDecision]);
  const itemKeys = useMemo(() => entries.map(entry => entry.key), [entries]);
  const estimateSize = useCallback(
    (_key: string, index: number) => estimatedRows(entries[index]!, contentWidth),
    [entries, contentWidth],
  );
  const virtual = useVirtualScroll({
    scrollRef,
    itemKeys,
    estimateSize,
    overscanRows: 20,
    minimumItems: 24,
    stickToEnd: Boolean(streaming),
    layoutKey: contentWidth,
  });

  useEffect(() => {
    const live = new Set(itemKeys);
    setExpandedKeys(previous => {
      if ([...previous].every(key => live.has(key))) return previous;
      return new Set([...previous].filter(key => live.has(key)));
    });
  }, [itemKeys]);

  if (entries.length === 0) {
    return <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1}>{empty}</ScrollBox>;
  }

  return (
    <ScrollBox ref={scrollRef} stickyScroll={Boolean(streaming)} flexDirection="column" flexGrow={1}>
      {virtual.before > 0 ? <Box height={virtual.before} flexShrink={0} /> : null}
      {entries.slice(virtual.start, virtual.end).map((entry, localIndex) => {
        const index = virtual.start + localIndex;
        const expanded = expandedKeys.has(entry.key);
        return (
          <Box
            key={entry.key}
            ref={virtual.itemRef(entry.key)}
            paddingTop={index > 0 ? 1 : 0}
            width="100%"
            flexShrink={0}
          >
            {entry.kind === "message" ? (
              entry.message.decision ? (
                <DecisionPanel
                  decision={entry.message.decision}
                  selectedIndex={decisionSelection}
                  composing={decisionComposing}
                  submitting={decisionSubmitting}
                  answer={entry.message.decision_answer}
                  active={entry.message.decision.decision_id === pendingDecision?.decision_id}
                />
              ) : (
                <MessageRow
                  msg={entry.message}
                  contentWidth={contentWidth}
                  verbose={verbose}
                  client={client}
                  project={project}
                  includeDescendants={includeDescendants}
                  expanded={expanded}
                  onExpandedChange={value => setExpandedKeys(previous => {
                    const next = new Set(previous);
                    if (value) next.add(entry.key);
                    else next.delete(entry.key);
                    return next;
                  })}
                />
              )
            ) : entry.kind === "stream" ? (
              <StreamingMessage
                streaming={entry.streaming}
                contentWidth={contentWidth}
                verbose={verbose}
              />
            ) : (
              <DecisionPanel
                decision={entry.decision}
                selectedIndex={Math.min(
                  decisionSelection,
                  Math.max(0, entry.decision.options.length - 1),
                )}
                composing={decisionComposing}
                submitting={decisionSubmitting}
              />
            )}
          </Box>
        );
      })}
      {virtual.after > 0 ? <Box height={virtual.after} flexShrink={0} /> : null}
    </ScrollBox>
  );
}
