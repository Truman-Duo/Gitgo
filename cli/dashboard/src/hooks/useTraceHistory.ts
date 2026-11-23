import {useCallback, useEffect, useMemo, useState} from "react";
import type {BackendClient} from "../backend/client.js";
import {cacheTrace, cachedTrace} from "../chat/traceCache.js";
import {readTraceIndex, traceBodyReader, type TraceSummary} from "../chat/traceSource.js";
import type {TraceRecord} from "../traceView.js";
import {useCopy} from "../ui/PageFrame.js";
import {useFeedback} from "./useFeedback.js";

type Index = {key: string; client: BackendClient; events: TraceRecord[]; complete: boolean};
export type TraceHistory = ReturnType<typeof useTraceHistory>;
export function useTraceHistory(client: BackendClient | undefined, project: string | undefined,
  traceId: string | undefined, processId: string, includeDescendants: boolean, status: string, enabled: boolean) {
  const t = useCopy();
  const key = JSON.stringify(["index-v1", project, traceId, processId, includeDescendants, status]);
  const [index, setIndex] = useState<Index>();
  const [summary, setSummary] = useState<{key: string; client: BackendClient; value: TraceSummary}>();
  const [failed, setFailed] = useState<{key: string; client: BackendClient}>();
  const [revision, setRevision] = useState(0);
  const retry = useCallback(() => setRevision(value => value + 1), []);
  const feedback = useFeedback({kind: "page", id: key});
  const terminal = ["completed", "failed", "cancelled", "timed_out", "degraded"].includes(status);
  const reader = useMemo(() => client && project && traceId ? traceBodyReader(client, project, traceId) : undefined,
    [client, project, traceId]);
  useEffect(() => {
    if (!enabled || !client || !project || !traceId) return;
    const controller = new AbortController();
    setFailed(undefined); feedback.clear();
    const saved = terminal ? cachedTrace(client, key) : null;
    if (saved) {setIndex({key, client, events: saved.events, complete: true}); return () => controller.abort();}
    setIndex(undefined); setSummary(undefined);
    void readTraceIndex(client, project, traceId, includeDescendants ? "" : processId, controller.signal,
      value => {setSummary({key, client, value});},
      (events, complete) => {
        setIndex({key, client, events, complete});
        if (terminal && complete) cacheTrace(client, key, {events, truncated: false});
      }).catch(error => {
        if (controller.signal.aborted) return;
        setFailed({key, client});
        feedback.show(t({en: "Could not read the trajectory: ", zh: "读取轨迹失败："}) + String(error), {tone: "error"});
      });
    return () => controller.abort();
  }, [enabled, client, project, traceId, processId, includeDescendants, key, terminal, revision, feedback.clear, feedback.show, t]);
  const unavailable = failed?.key === key && failed.client === client;
  return {
    index: index?.key === key && index.client === client ? index : undefined,
    summary: summary?.key === key && summary.client === client ? summary.value : undefined,
    readBody: enabled ? reader : undefined,
    unavailable, feedback: unavailable ? feedback.message : undefined, retry,
  };
}
