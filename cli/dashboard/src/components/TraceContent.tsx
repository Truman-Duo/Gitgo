import React, {useEffect, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import type {TraceBodyReader, TraceBodyRef, TraceDetailReader} from "../chat/traceSource.js";
import type {TraceRecord} from "../traceView.js";
import {useFeedback} from "../hooks/useFeedback.js";
import {useCopy} from "../ui/PageFrame.js";
import {FeedbackMessage} from "./FeedbackMessage.js";

/** Mounted only for a visible/expanded record. Input and disclosure stay with
 * the owner. An unmount or source change invalidates every pending page. */
type EventsProps = {
  loadingRows?:number;
  references?: TraceBodyRef[]; read?: TraceBodyReader;
  children: (events: TraceRecord[] | null) => React.ReactNode;
};
type DetailProps = {detailRef: string; readDetail?: TraceDetailReader; loadingRows?:number; children: (detail: unknown) => React.ReactNode};
export function TraceContent(props: EventsProps): React.ReactNode;
export function TraceContent(props: DetailProps): React.ReactNode;
export function TraceContent(props: EventsProps | DetailProps) {
  const t = useCopy();
  const detail = "detailRef" in props;
  const source = detail ? props.detailRef : props.references;
  const read = detail ? props.readDetail : props.read;
  const identity = JSON.stringify([detail ? "detail" : "events",source || []]);
  const [body, setBody] = useState<{identity: string; read: object; content: unknown}>();
  const [unavailable, setUnavailable] = useState<{identity: string; read: object}>();
  const feedback = useFeedback({kind: "page", id: identity});
  const needed = Boolean(read && source?.length);
  const loaded = body?.identity === identity && body.read === read;
  const content = loaded ? body.content : null;
  const failed = unavailable?.identity === identity && unavailable.read === read;
  useEffect(() => {
    if (!read || !source?.length) return;
    const controller = new AbortController();
    setUnavailable(undefined); feedback.clear();
    const pending = detail ? (read as TraceDetailReader)(source as string,controller.signal)
      : (read as TraceBodyReader)(source as TraceBodyRef[],controller.signal);
    void pending.then(value => {
      if (!controller.signal.aborted) setBody({identity, read, content: value});
    }).catch(error => {
      if (controller.signal.aborted) return;
      setUnavailable({identity, read});
      feedback.show(t({en: "Could not read the recorded content: ", zh: "读取已记录正文失败："}) + String(error), {tone: "error"});
    });
    return () => controller.abort();
  }, [identity, read, feedback.clear, feedback.show, t]);
  return <Box flexDirection="column">
    {(props.children as (value: any) => React.ReactNode)(content)}
    {needed && !loaded && !failed ? <Box minHeight={Math.max(1,props.loadingRows || 1)} flexShrink={0}><Text dimColor>
      {t({en: "Loading recorded content…", zh: "正在读取已记录正文…"})}
    </Text></Box> : null}
    {failed ? <Text dimColor>
      {t({en: "Content unavailable. Fold and reopen this record to retry.", zh: "正文暂不可用，收起并重新展开可重试。"})}
    </Text> : null}
    <FeedbackMessage feedback={failed ? feedback.message : undefined}/>
  </Box>;
}
