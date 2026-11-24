import {useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {colors} from "../theme/index.js";

export type FeedbackScope = {kind: "page" | "project"; id: string} | {kind: "global"};
export type FeedbackTone = "info" | "success" | "warning" | "error";
export type FeedbackRetention = {kind: "timed"; durationMs?: number}
  | {kind: "until-resolved"; reason: "operation-pending" | "blocking-state"};
export type FeedbackOptions = {tone?: FeedbackTone; retention?: FeedbackRetention};
export type Feedback = {text: string; tone: FeedbackTone; owner: string; id: number; expiresAt?: number};

/** Transient feedback belongs to its declared owner, never to an arbitrary next page.
 * Project/global owners must be deliberately chosen by the caller. Persistent
 * informational messages require an explicit unresolved operation/state.
 * Warning/error results always expire; durable business facts stay in their view.
 */
export function useFeedback(scope: FeedbackScope, defaults: FeedbackOptions = {}) {
  const key = scope.kind + ":" + ("id" in scope ? scope.id : "*");
  // Each visit owns a fresh token. A -> B -> A must not reactivate A's
  // original asynchronous callbacks just because its string ID matches again.
  const visit = useMemo(() => ({key, active: true}), [key]);
  const owner = useRef(visit), sequence = useRef(0);
  const [feedback, setFeedback] = useState<Feedback>();
  const defaultTone = defaults.tone ?? "info";
  const retentionKind = defaults.retention?.kind ?? "timed";
  const defaultDuration = defaults.retention?.kind === "timed" ? defaults.retention.durationMs : undefined;
  useLayoutEffect(() => {
    if (owner.current !== visit) setFeedback(undefined);
    owner.current = visit; visit.active = true;
    return () => {visit.active = false;};
  }, [visit]);
  const show = useCallback((text: string, options: FeedbackOptions = {}) => {
    // A callback captured by an earlier page cannot publish into the new page.
    if (!visit.active || owner.current !== visit) return;
    if (!text) {setFeedback(undefined); return;}
    const tone = options.tone ?? defaultTone;
    const requested = options.retention ?? (retentionKind === "until-resolved"
      ? {kind: "until-resolved" as const} : {kind: "timed" as const, durationMs: defaultDuration});
    const retention = tone !== "info" && requested.kind === "until-resolved" ? {kind: "timed" as const} : requested;
    const duration = retention.kind === "timed" ? retention.durationMs ?? colors.feedback[`${tone}Ms`] : undefined;
    const ttl = duration === undefined ? undefined : Number.isFinite(duration) ? duration : colors.feedback[`${tone}Ms`];
    setFeedback({text, tone, owner: key, id: ++sequence.current,
      ...(ttl !== undefined ? {expiresAt: Date.now() + Math.max(1, ttl)} : {})});
  }, [key, visit, defaultTone, retentionKind, defaultDuration]);
  const clear = useCallback(() => show(""), [show]);
  const message = feedback?.owner === key ? feedback : undefined;
  useEffect(() => {
    if (!message || message.expiresAt === undefined) return;
    const timer = setTimeout(() => setFeedback(value => value?.id === message.id ? undefined : value), Math.max(1, message.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [message]);
  return {message, text: message?.text ?? "", show, clear};
}
