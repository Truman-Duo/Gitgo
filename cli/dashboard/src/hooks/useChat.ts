// src/hooks/useChat.ts — Chat message state + send to agent.
// Keeps only state (messages + transient streaming row) and re-seeding; the
// actual transport selection + stream parsing lives in src/chat/sendChat.ts.
import { useState, useCallback, useEffect, useRef } from "react";
import type { BackendClient } from "../backend/client.js";
import type { ChatMessage, PendingDecision, StreamingRow } from "../types.js";
import { sendChat } from "../chat/sendChat.js";

function stableMessageKey(message: ChatMessage): string {
  return message.message_id || message.id ||
    `${message.timestamp}:${message.role}:${message.content}`;
}

/** Merge a polled durable transcript with rows still awaiting persistence. */
export function reconcileChatMessages(
  seed: ChatMessage[] | null | undefined,
  current: ChatMessage[],
): ChatMessage[] {
  const canonical: ChatMessage[] = [];
  const canonicalIds = new Set<string>();
  for (const message of seed ?? []) {
    const key = stableMessageKey(message);
    if (canonicalIds.has(key)) continue;
    canonicalIds.add(key);
    canonical.push(message);
  }

  const optimistic = current.filter((message) =>
    message.message_id?.startsWith("optimistic:") || message.awaiting_persistence
    || message.kind === "knowledge_notice",
  );
  for (const pending of optimistic) {
    const matchedIndex = canonical.findIndex((message) =>
      stableMessageKey(message) === stableMessageKey(pending) || (
        !pending.decision && !message.decision &&
        message.role === pending.role && message.content === pending.content
        && ((!pending.turn_id || pending.turn_id.startsWith("optimistic:"))
          ? Date.parse(message.timestamp) >= Date.parse(pending.timestamp)
          : message.turn_id === pending.turn_id)
      ),
    );
    if (matchedIndex >= 0) {
      const durable = canonical[matchedIndex];
      // Host acceptance can arrive before the polling snapshot. Decision IDs,
      // not identical question text, identify cards; keep the acknowledged
      // answer until the durable ledger includes it.
      const answerPending = Boolean(pending.decision && pending.decision_answer
        && !durable.decision_answer);
      // Tool cards and compact notices are live decorations. Preserve them on
      // the now-authoritative durable row without creating a second message.
      canonical[matchedIndex] = {
        ...canonical[matchedIndex],
        tools: pending.tools ?? canonical[matchedIndex].tools,
        notices: pending.notices ?? canonical[matchedIndex].notices,
        reasoning: pending.reasoning ?? canonical[matchedIndex].reasoning,
        activity: pending.activity ?? canonical[matchedIndex].activity,
        provider_usage: pending.provider_usage ?? canonical[matchedIndex].provider_usage,
        duration_ms: canonical[matchedIndex].duration_ms ?? pending.duration_ms,
        status: answerPending ? pending.status : canonical[matchedIndex].status ?? pending.status,
        decision_answer: answerPending ? pending.decision_answer : durable.decision_answer,
        trace_id: canonical[matchedIndex].trace_id || pending.trace_id,
        process_id: canonical[matchedIndex].process_id || pending.process_id,
        awaiting_persistence: answerPending,
      };
    } else {
      canonical.push(pending);
    }
  }
  // Once an optimistic outcome becomes durable, its trace decorations remain
  // local UI state. Reconcile them by stable identity on every later poll so
  // the completed capsule does not lose its timeline before/after expansion.
  for (let index = 0; index < canonical.length; index++) {
    const local = current.find(message => stableMessageKey(message) === stableMessageKey(canonical[index]));
    if (!local) continue;
    canonical[index] = {
      ...canonical[index],
      tools: local.tools ?? canonical[index].tools,
      notices: local.notices ?? canonical[index].notices,
      reasoning: local.reasoning ?? canonical[index].reasoning,
      activity: local.activity ?? canonical[index].activity,
      provider_usage: local.provider_usage ?? canonical[index].provider_usage,
    };
  }
  return canonical;
}

export function knowledgeHarvestMessage(
  project: string,
  event: Record<string, any>,
): ChatMessage {
  const count = Math.max(0, Number(event.count || 0));
  const identity = String(event.harvest_id || event.time || `${Date.now()}`);
  return {
    message_id: `knowledge:${project}:${identity}`,
    turn_id: identity,
    role: "system",
    kind: "knowledge_notice",
    content: count > 0
      ? `Knowledge harvested · ${count} pending lesson${count === 1 ? "" : "s"}. Review in /runtime lesson.`
      : "Knowledge review complete · no new lesson added.",
    timestamp: String(event.time || new Date().toISOString()),
    final: true,
  };
}

// Streaming text lives in a separate transient row (never inside `messages`),
// so the authoritative poll snapshot can wholesale-replace `messages` without
// clobbering an in-flight token stream. On complete (or error) the transient
// row is dissolved and the final assistant message is committed to `messages`.
export function useChat(
  client: BackendClient,
  project: string,
  seed?: ChatMessage[] | null,
  recoveredDecision?: PendingDecision | null,
) {
  const [messages, setMessages] = useState<ChatMessage[]>(seed ?? []);
  const [streaming, setStreaming] = useState<StreamingRow | null>(null);
  const [pendingDecision, setPendingDecision] = useState<PendingDecision | null>(null);
  const [activeProcessId, setActiveProcessId] = useState("");
  const [activeRequestId, setActiveRequestId] = useState("");
  const inFlightRef = useRef(false);
  const decisionInFlightRef = useRef(false);
  const answeredDecisions = useRef(new Set<string>());
  const observedDecision = useRef("");

  // Re-seed when the injected conversation changes (e.g. switching B agent).
  useEffect(() => {
    setMessages((current) => reconcileChatMessages(seed, current));
  }, [seed]);

  useEffect(() => {
    if (!client?.subscribeEvents || !project) return;
    const unsubscribe = client.subscribeEvents((event) => {
      if (String(event.project || "") !== project || event.event !== "lessons_harvested") return;
      const notice = knowledgeHarvestMessage(project, event);
      setMessages(current => current.some(row => row.message_id === notice.message_id) ? current : [
        ...current, notice,
      ]);
    });
    return unsubscribe;
  }, [client, project]);

  // A decision is daemon-owned state, not a transient Dashboard prompt.  When
  // the Dashboard reconnects, restore the full routing identity so the next
  // NORMAL-bar reply cannot be mistaken for an ordinary root task.
  useEffect(() => {
    if (recoveredDecision === undefined) return; // Poll not available yet.
    if (!recoveredDecision) {
      const previous = observedDecision.current;
      observedDecision.current = "";
      if (!inFlightRef.current && previous) {
        setPendingDecision(current => current?.decision_id === previous ? null : current);
      }
      return;
    }
    if (answeredDecisions.current.has(recoveredDecision.decision_id)) return;
    observedDecision.current = recoveredDecision.decision_id;
    setPendingDecision((current) => {
      if (
        current?.task_id === recoveredDecision.task_id
        && current.process_id === recoveredDecision.process_id
        && current.decision_id === recoveredDecision.decision_id
      ) {
        return current;
      }
      return recoveredDecision;
    });
  }, [recoveredDecision]);

  const send = useCallback(async (text: string, options: {manualDelegation?: boolean} = {}) => {
    const isDecisionSubmission = Boolean(pendingDecision);
    if (
      !text.trim()
      || (isDecisionSubmission ? decisionInFlightRef.current : inFlightRef.current)
    ) return;
    const routedChildDecision = Boolean(
      pendingDecision?.source_process_id
      && pendingDecision?.owner_process_id
      && pendingDecision.source_process_id !== pendingDecision.owner_process_id
    );
    if (isDecisionSubmission) decisionInFlightRef.current = true;
    else inFlightRef.current = true;
    const startTime = new Date().toISOString();
    const optimisticTurn = `optimistic:${startTime}`;
    if (!pendingDecision) setMessages((prev) => [...prev, {
      message_id: `${optimisticTurn}:user`,
      turn_id: optimisticTurn,
      role: "user", content: text, timestamp: startTime, final: true,
    }]);
    if (!routedChildDecision) {
      setStreaming({ text: "", reasoning: "", tools: [], notices: [], timestamp: startTime });
    }

    try {
      await sendChat(client, project, text, startTime, pendingDecision, {
        onAcknowledged: (ack) => {
          // A child decision resumes inside the already-live root request. Do
          // not replace A's interrupt/correlation identity with the child.
          if (!routedChildDecision) {
            if (ack.requestId) setActiveRequestId(ack.requestId);
            if (ack.processId) setActiveProcessId(ack.processId);
          }
          if (pendingDecision) {
            answeredDecisions.current.add(pendingDecision.decision_id);
            setMessages(prev => prev.map(row => row.decision?.decision_id === pendingDecision.decision_id
              ? {...row, decision_answer: text, status: "answered", awaiting_persistence: true} : row));
            // The Host has durably accepted the choice. Collapse the card now;
            // resumed tool/model work continues as later timeline activity.
            setPendingDecision(null);
          }
        },
        onStream: routedChildDecision ? () => undefined : setStreaming,
        onDone: (content, tools, notices, nextDecision, round) => {
          if (pendingDecision) answeredDecisions.current.add(pendingDecision.decision_id);
          if (nextDecision) {
            setMessages(prev => [...prev.map(row => row.decision?.decision_id === pendingDecision?.decision_id
              && row.decision ? {...row, decision_answer: text, status: "answered", awaiting_persistence: true} : row)
              .filter(row => row.message_id !== `decision:${nextDecision.decision_id}`), {
                message_id: `decision:${nextDecision.decision_id}`, turn_id: nextDecision.task_id,
                role: "assistant", kind: "decision", content: nextDecision.question,
                decision: nextDecision, status: "awaiting_user", final: true,
                awaiting_persistence: true, timestamp: nextDecision.created_at || new Date().toISOString(),
              }]);
            setPendingDecision(nextDecision); setActiveProcessId(""); setActiveRequestId(""); setStreaming(null);
            return;
          }
          if (pendingDecision) setMessages(prev => prev.map(row => row.decision?.decision_id === pendingDecision.decision_id
            ? {...row, decision_answer: text, status: "answered", awaiting_persistence: true} : row));
          if (routedChildDecision) {
            setPendingDecision(nextDecision ?? null);
            return;
          }
          setMessages((prev) => [...prev.filter(row => !round?.task_id || row.message_id !== `outcome:${round.task_id}`), {
            message_id: round?.task_id ? `outcome:${round.task_id}` : `${optimisticTurn}:assistant`,
            turn_id: round?.task_id || optimisticTurn,
            awaiting_persistence: true,
            role: "assistant",
            content,
            timestamp: new Date().toISOString(),
            tools: tools && tools.length > 0 ? tools : undefined,
            notices: notices && notices.length > 0 ? notices : undefined,
            reasoning: round?.reasoning,
            activity: round?.activity,
            provider_usage: round?.provider_usage,
            duration_ms: Math.max(
              Number(round?.duration_ms || 0),
              Math.max(0, Date.now() - Date.parse(startTime)),
            ),
            status: round?.status || "completed",
            trace_id: round?.trace_id,
            process_id: round?.process_id,
            kind: round?.status === "degraded" ? "error" : "outcome",
            final: true,
          }]);
          setPendingDecision(nextDecision ?? null);
          setActiveProcessId("");
          setActiveRequestId("");
          setStreaming(null);
        },
        onError: (message, round) => {
          if (routedChildDecision) {
            setMessages(prev => [...prev, {
              message_id: `child-decision-error:${pendingDecision?.decision_id}:${startTime}`,
              turn_id: pendingDecision?.task_id || optimisticTurn,
              kind: "error", role: "assistant",
              content: `[Error: B question could not resume: ${message}]`,
              timestamp: new Date().toISOString(), final: true,
            }]);
            return;
          }
          setMessages((prev) => [...prev.filter(row => !round?.task_id || row.message_id !== `outcome:${round.task_id}`), {
            // A pre-admission transport error has no durable backend twin.  Do
            // not mark it as an optimistic row awaiting persistence forever;
            // the next authoritative transcript poll may replace it.
            message_id: round?.task_id ? `outcome:${round.task_id}` : `transport-error:${startTime}`,
            turn_id: round?.task_id || optimisticTurn,
            awaiting_persistence: Boolean(round?.task_id),
            kind: "error",
            role: "assistant",
            content: `[Error: ${message}]`,
            timestamp: new Date().toISOString(),
            duration_ms: Math.max(
              Number(round?.duration_ms || 0),
              Math.max(0, Date.now() - Date.parse(startTime)),
            ),
            status: round?.status || "failed",
            trace_id: round?.trace_id,
            process_id: round?.process_id,
            reasoning: round?.reasoning,
            activity: round?.activity,
            provider_usage: round?.provider_usage,
            final: true,
          }]);
          setActiveProcessId("");
          setActiveRequestId("");
          setStreaming(null);
        },
      }, options);
    } finally {
      if (isDecisionSubmission) decisionInFlightRef.current = false;
      else inFlightRef.current = false;
    }
  }, [client, project, pendingDecision]);

  const submitManual = useCallback((text: string): boolean => {
    if (!text.trim() || inFlightRef.current || pendingDecision) return false;
    void send(text, {manualDelegation: true});
    return true;
  }, [send, pendingDecision]);
  return { messages, streaming, pendingDecision, activeProcessId, activeRequestId, send, submitManual };
}
