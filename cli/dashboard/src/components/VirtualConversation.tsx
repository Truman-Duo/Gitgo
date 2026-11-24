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
import {DecisionSummary} from "./DecisionSummary.js";
import { MessageRow } from "./MessageRow.js";
import { StreamingMessage } from "./StreamingMessage.js";
import {projectConversationRounds} from "../chat/conversationRounds.js";
import {ContentViewport} from "../ui/ContentViewport.js";
import {expandedValue, type ExpandedChange} from "../hooks/useDisclosureState.js";
import {createDisclosureStore,DisclosureScope} from '../hooks/disclosureScope.js';
import {ViewportScrollbar} from './ViewportScrollbar.js';

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

const decisionKey=(decision:PendingDecision)=>"decision:"+JSON.stringify([decision.task_id,decision.process_id,decision.decision_id]);
const sameDecision=(left:PendingDecision,right:PendingDecision|null|undefined):right is PendingDecision=>Boolean(right && decisionKey(left)===decisionKey(right));

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
  if(message.kind==="decision_summary")return 5;
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
  const bodyWidth=Math.max(1,contentWidth-1);
  const [expandedKeys, setExpandedKeys] = useState<Map<string,boolean>>(
    () => new Map(),
  );
  const [disclosures]=useState(createDisclosureStore);
  useEffect(()=>{
    // Also retire private readers in virtual rows which are currently absent.
    if(!verbose)disclosures.clearMatching(key=>key.slice(key.indexOf('/')+1).startsWith('timeline/context:'));
  },[verbose,disclosures]);
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
    for (const [index, message] of projectConversationRounds(messages, streaming).entries()) {
      if ((message.turn_id && message.turn_id !== turnKey) || message.role === "user") {
        turnKey = message.turn_id || messageKey(message, index);
        replyIndex = 0;
      }
      const key =
        replyAliases.current.get(messageKey(message, index)) ||
        (message.decision
          ? decisionKey(message.decision)
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
          message.decision && sameDecision(message.decision,pendingDecision),
      )
    ) {
      result.push({
        kind: "decision",
        key: decisionKey(pendingDecision),
        decision: pendingDecision,
        turnKey,
      });
    }
    return result;
  }, [messages, streaming, pendingDecision]);
  const itemKeys = useMemo(() => entries.map((entry) => entry.key), [entries]);
  const estimateSize = useCallback(
    (_key: string, index: number) =>
      estimatedRows(entries[index]!, bodyWidth),
    [entries, bodyWidth],
  );
  const virtual = useVirtualScroll({
    scrollRef,
    itemKeys,
    estimateSize,
    overscanRows: 20,
    minimumItems: 24,
    stickToEnd: true,
    layoutKey: bodyWidth,
  });

  useEffect(() => {
    const live = new Set(itemKeys);
    disclosures.prune(itemKeys.map(key=>encodeURIComponent(key)));
    setExpandedKeys((previous) => {
      const next = new Map([...previous].filter(([key]) => live.has(key)));
      if (pendingDecision) next.set(decisionKey(pendingDecision),true);
      return next.size === previous.size &&
        [...next].every(([key,value]) => previous.get(key)===value)
        ? previous
        : next;
    });
  }, [itemKeys, pendingDecision?.decision_id]);
  const setExpanded = (key: string, value: ExpandedChange, defaultValue=false) =>
    setExpandedKeys((previous) => {
      const next = new Map(previous);
      next.set(key,expandedValue(previous.get(key) ?? defaultValue,value));
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
  return (
    <ContentViewport scrollRef={scrollRef}><ViewportScrollbar scrollRef={scrollRef}><ScrollBox
      ref={scrollRef}
      stickyScroll
      flexDirection="column"
      flexGrow={1}
      minWidth={0}
    >
      {entries.length===0?empty:null}
      {virtual.before > 0 ? (
        <Box height={virtual.before} flexShrink={0} />
      ) : null}
      {entries.slice(virtual.start, virtual.end).map((entry, localIndex) => {
        const index = virtual.start + localIndex;
        const expanded = expandedKeys.get(entry.key) ?? entry.kind === "stream";
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
            <DisclosureScope store={disclosures} scope={encodeURIComponent(entry.key)}><TurnGuide
              first={first}
              active={!afterEnd || liveTurn}
              status={turnStatus}
              last={last && !liveTurn && !(pendingDecision && entry.turnKey === entries[entries.length - 1]?.turnKey)}
            >
              {entry.kind === "message" ? (
                entry.message.decision ? (
                  entry.message.kind==="decision_summary" && entry.message.decision_answer
                  ? <DecisionSummary decision={entry.message.decision} answer={entry.message.decision_answer}/>
                  :
                  <DecisionPanel
                    decision={
                      sameDecision(entry.message.decision,pendingDecision)
                        ? pendingDecision
                        : entry.message.decision
                    }
                    selectedIndex={decisionSelection}
                    composing={decisionComposing}
                    submitting={decisionSubmitting}
                    answer={entry.message.decision_answer}
                    active={
                      sameDecision(entry.message.decision,pendingDecision)
                    }
                    expanded={expanded}
                    onExpandedChange={(value) => setExpanded(entry.key, value)}
                  />
                ) : (
                  <ReplyMessage
                    msg={entry.message}
                    contentWidth={Math.max(1,bodyWidth - 2)}
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
                  client={client}
                  project={project}
                  contentWidth={Math.max(1,bodyWidth - 2)}
                  verbose={verbose}
                  expanded={expanded}
                  onExpandedChange={(value) => setExpanded(entry.key,value,true)}
                  waiting={Boolean(pendingDecision)}
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
            </TurnGuide></DisclosureScope>
          </Box>
        );
      })}
      {virtual.after > 0 ? <Box height={virtual.after} flexShrink={0} /> : null}
    </ScrollBox></ViewportScrollbar></ContentViewport>
  );
}

/** Keep one reply identity from live rendering to its final answer. */
function ReplyMessage(
  props: Omit<React.ComponentProps<typeof MessageRow>, "msg"> & {
    msg?: ChatMessage;
    streaming?: StreamingRow;
    waiting?: boolean;
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
        client={props.client}
        project={props.project}
        expanded={props.expanded}
        onExpandedChange={props.onExpandedChange}
        waiting={props.waiting}
      />
    );
  const { msg, streaming: _streaming, waiting: _waiting, ...rest } = props;
  return (
    <MessageRow {...rest} msg={msg!} streamSnapshot={previousStream.current} />
  );
}
