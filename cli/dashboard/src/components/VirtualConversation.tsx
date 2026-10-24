import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Box, ScrollBox, useVirtualScroll } from "@anthropic/ink";
import type { ScrollBoxHandle } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import type { ChatMessage, PendingDecision, StreamingRow } from "../types.js";
import { TurnGuide } from "./TurnGuide.js";
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
  return (
    message.message_id ||
    message.id ||
    `${message.timestamp}-${message.role}-${message.content.slice(0, 32)}-${index}`
  );
}

function estimatedRows(entry: ConversationEntry, width: number): number {
  const usable = Math.max(12, width - 4);
  if (entry.kind === "decision") {
    return Math.max(5, 4 + entry.decision.options.length * 2);
  }
  if (entry.kind === "stream") {
    const text = `${entry.streaming.reasoning || ""}\n${entry.streaming.text || ""}`;
    return Math.max(
      4,
      3 + Math.ceil(text.length / usable) + entry.streaming.tools.length * 2,
    );
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
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(
    () => new Set(),
  );
  // UI aliases bridge optimistic-to-durable identities; they never rewrite stored messages.
  const replyAliases = useRef(new Map<string, string>());
  const liveReply = useRef<{
    stamp: string;
    key: string;
    known: Set<string>;
  } | null>(null);
  const entries = useMemo<
    Array<ConversationEntry & { turnKey: string }>
  >(() => {
    const result: Array<ConversationEntry & { turnKey: string }> = [];
    if (streaming && liveReply.current?.stamp !== streaming.timestamp) {
      liveReply.current = {
        stamp: streaming.timestamp,
        key: `live-reply:${streaming.timestamp}`,
        known: new Set(messages.map(messageKey)),
      };
    }
    let turnKey = "history",
      replyIndex = 0;
    for (const [index, message] of messages.entries()) {
      if (message.role === "user") {
        turnKey = messageKey(message, index);
        replyIndex = 0;
      }
      const key =
        replyAliases.current.get(messageKey(message, index)) ||
        (message.decision
          ? `decision:${message.decision.decision_id}`
          : message.role === "assistant"
            ? `reply:${turnKey}:${replyIndex++}`
            : `message:${messageKey(message, index)}`);
      result.push({
        kind: "message",
        key,
        message,
        messageIndex: index,
        turnKey,
      });
    }
    if (streaming)
      result.push({
        kind: "stream",
        key: liveReply.current!.key,
        streaming,
        turnKey,
      });
    if (!streaming && liveReply.current) {
      const completed = [...result]
        .reverse()
        .find(
          (entry) =>
            entry.kind === "message" &&
            entry.message.role === "assistant" &&
            !entry.message.decision &&
            entry.message.status &&
            !liveReply.current!.known.has(
              messageKey(entry.message, entry.messageIndex),
            ),
        );
      if (completed?.kind === "message") {
        replyAliases.current.set(
          messageKey(completed.message, completed.messageIndex),
          liveReply.current.key,
        );
        completed.key = liveReply.current.key;
        liveReply.current = null;
      }
    }
    if (
      pendingDecision &&
      !messages.some(
        (message) =>
          message.decision?.decision_id === pendingDecision.decision_id,
      )
    ) {
      result.push({
        kind: "decision",
        key: `decision:${pendingDecision.decision_id}`,
        decision: pendingDecision,
        turnKey,
      });
    }
    return result;
  }, [messages, streaming, pendingDecision]);
  const itemKeys = useMemo(() => entries.map((entry) => entry.key), [entries]);
  const estimateSize = useCallback(
    (_key: string, index: number) =>
      estimatedRows(entries[index]!, contentWidth),
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
    setExpandedKeys((previous) => {
      const next = new Set([...previous].filter((key) => live.has(key)));
      if (pendingDecision) next.add(`decision:${pendingDecision.decision_id}`);
      return next.size === previous.size &&
        [...next].every((key) => previous.has(key))
        ? previous
        : next;
    });
  }, [itemKeys, pendingDecision?.decision_id]);
  const setExpanded = (key: string, value: boolean) =>
    setExpandedKeys((previous) => {
      const next = new Set(previous);
      if (value) next.add(key);
      else next.delete(key);
      return next;
    });

  const turnEnds = useMemo(() => {
    const result = new Map<string, {position: number; status: string}>();
    entries.forEach((item, position) => {
      if (item.kind === "message" && !item.message.decision &&
          ["completed", "failed", "cancelled", "timed_out", "degraded"].includes(item.message.status || "")) {
        result.set(item.turnKey, {position, status: item.message.status!});
      }
    });
    return result;
  }, [entries]);
  if (entries.length === 0) {
  return (
      <ScrollBox ref={scrollRef} flexDirection="column" flexGrow={1}>
        {empty}
      </ScrollBox>
    );
  }

  return (
    <ScrollBox
      ref={scrollRef}
      stickyScroll={Boolean(streaming)}
      flexDirection="column"
      flexGrow={1}
    >
      {virtual.before > 0 ? (
        <Box height={virtual.before} flexShrink={0} />
      ) : null}
      {entries.slice(virtual.start, virtual.end).map((entry, localIndex) => {
        const index = virtual.start + localIndex;
        const expanded = expandedKeys.has(entry.key);
        const first =
          index === 0 || entries[index - 1]?.turnKey !== entry.turnKey;
        const terminal = turnEnds.get(entry.turnKey);
        const turnStatus = terminal?.status;
        const last = terminal?.position === index;
        const afterEnd = terminal !== undefined && index > terminal.position;
        const liveTurn =
          Boolean(streaming) &&
          entry.turnKey ===
            entries.find((item) => item.kind === "stream")?.turnKey;
        return (
          <Box
            key={entry.key}
            ref={virtual.itemRef(entry.key)}
            paddingTop={index > 0 && first ? 1 : 0}
            flexDirection="column"
            width="100%"
            flexShrink={0}
          >
            <TurnGuide
              first={first}
              active={!afterEnd || liveTurn}
              status={turnStatus}
              last={last && !liveTurn && !(pendingDecision && entry.turnKey === entries[entries.length - 1]?.turnKey)}
            >
              {entry.kind === "message" ? (
                entry.message.decision ? (
                  <DecisionPanel
                    decision={
                      entry.message.decision.decision_id ===
                      pendingDecision?.decision_id
                        ? pendingDecision
                        : entry.message.decision
                    }
                    selectedIndex={decisionSelection}
                    composing={decisionComposing}
                    submitting={decisionSubmitting}
                    answer={entry.message.decision_answer}
                    active={
                      entry.message.decision.decision_id ===
                      pendingDecision?.decision_id
                    }
                    expanded={expanded}
                    onExpandedChange={(value) => setExpanded(entry.key, value)}
                  />
                ) : (
                  <ReplyMessage
                    msg={entry.message}
                    contentWidth={contentWidth - 2}
                    verbose={verbose}
                    client={client}
                    project={project}
                    includeDescendants={includeDescendants}
                    expanded={expanded}
                    onExpandedChange={(value) => setExpanded(entry.key, value)}
                  />
                )
              ) : entry.kind === "stream" ? (
                <ReplyMessage
                  streaming={entry.streaming}
                  contentWidth={contentWidth - 2}
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
                  expanded={expanded}
                  onExpandedChange={(value) => setExpanded(entry.key, value)}
                />
              )}
            </TurnGuide>
          </Box>
        );
      })}
      {virtual.after > 0 ? <Box height={virtual.after} flexShrink={0} /> : null}
    </ScrollBox>
  );
}

/** Keep one reply identity from live rendering to its final answer. */
function ReplyMessage(
  props: Omit<React.ComponentProps<typeof MessageRow>, "msg"> & {
    msg?: ChatMessage;
    streaming?: StreamingRow;
  },
) {
  const previousStream = useRef<StreamingRow | undefined>(props.streaming);
  useEffect(() => {
    if (props.streaming) previousStream.current = props.streaming;
  }, [props.streaming]);
  if (props.streaming)
    return (
      <StreamingMessage
        streaming={props.streaming}
        contentWidth={props.contentWidth}
        verbose={props.verbose}
      />
    );
  const { msg, streaming: _streaming, ...rest } = props;
  return (
    <MessageRow {...rest} msg={msg!} streamSnapshot={previousStream.current} />
  );
}
