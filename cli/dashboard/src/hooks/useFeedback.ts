import {useCallback, useEffect, useLayoutEffect, useRef, useState} from "react";
import {colors} from "../theme/index.js";

export type FeedbackScope = {kind: "page" | "project"; id: string} | {kind: "global"};
export type FeedbackTone = "info" | "success" | "warning" | "error";
export type FeedbackRetention = {kind: "timed"; durationMs?: number}
  | {kind: "until-resolved"; reason: "operation-pending" | "blocking-state"};
export type FeedbackOptions = {tone?: FeedbackTone; retention?: FeedbackRetention};
export type Feedback = {text: string; tone: FeedbackTone; owner: string; id: number; expiresAt?: number};

/** Transient feedback belongs to its declared owner, never to an arbitrary next page.
 * Project/global owners must be deliberately chosen by the caller. Persistent
 * messages require an explicit unresolved operation/state; they are not toasts.
 */
export function useFeedback(scope: FeedbackScope, defaults: FeedbackOptions = {}) {
  const key = scope.kind + ":" + ("id" in scope ? scope.id : "*");
  const owner = useRef({key, active: true}), sequence = useRef(0);
  const [feedback, setFeedback] = useState<Feedback>();
  const defaultTone = defaults.tone ?? "info";
  const retentionKind = defaults.retention?.kind ?? "timed";
  const defaultDuration = defaults.retention?.kind === "timed" ? defaults.retention.durationMs : undefined;
  useLayoutEffect(() => {
    if (owner.current.key !== key) setFeedback(undefined);
    owner.current = {key, active: true};
    return () => {owner.current.active = false;};
  }, [key]);
  const show = useCallback((text: string, options: FeedbackOptions = {}) => {
    // A callback captured by an earlier page cannot publish into the new page.
    if (!owner.current.active || owner.current.key !== key) return;
    if (!text) {setFeedback(undefined); return;}
    const tone = options.tone ?? defaultTone;
    const retention = options.retention ?? (retentionKind === "until-resolved"
      ? {kind: "until-resolved" as const} : {kind: "timed" as const, durationMs: defaultDuration});
    const ttl = retention.kind === "timed" ? retention.durationMs ?? colors.feedback[`${tone}Ms`] : undefined;
    setFeedback({text, tone, owner: key, id: ++sequence.current,
      ...(ttl !== undefined ? {expiresAt: Date.now() + Math.max(1, ttl)} : {})});
  }, [key, defaultTone, retentionKind, defaultDuration]);
  const clear = useCallback(() => show(""), [show]);
  const message = feedback?.owner === key ? feedback : undefined;
  useEffect(() => {
    if (!message || message.expiresAt === undefined) return;
    const timer = setTimeout(() => setFeedback(value => value?.id === message.id ? undefined : value), Math.max(1, message.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [message]);
  return {message, text: message?.text ?? "", show, clear};
}
