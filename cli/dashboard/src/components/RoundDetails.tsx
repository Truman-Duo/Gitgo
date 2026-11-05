// Historical trajectories are read only when opened, one bounded page at a time.
// They never enter the provider context and never become a second event writer.
import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import { runtimeTrace } from "../backend/tools.js";
import type { ChatMessage, StreamActivity } from "../types.js";
import { roundTraceEvents } from "../chat/roundTrajectory.js";
import type { TraceRecord } from "../traceView.js";
import { colors } from "../theme/index.js";
import {cachedTrace, cacheTrace, traceCacheKey} from "../chat/traceCache.js";
import { timelineFromTrace } from "../chat/turnTimeline.js";
import { TurnTimeline } from "./TurnTimeline.js";
import {useCopy} from "../ui/PageFrame.js";

function EventDetail({ detailRef, client, project }: {
  detailRef: string; client: BackendClient; project: string;
}) {
  const t=useCopy();
  const [expanded, setExpanded] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!expanded || body !== null || !detailRef) return;
    let alive = true;
    setError("");
    void runtimeTrace(client, project, { action: "detail", ref: detailRef })
      .then((result: any) => {
        if (alive) setBody(JSON.stringify(result.detail, null, 2) ?? t({en:"No saved detail",zh:"没有保存的详情"}));
      })
      .catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [expanded, body, detailRef, client, project, t]);
  return <Box flexDirection="column">
    <Box onClick={() => setExpanded(value => !value)}>
      <Text dimColor>{expanded ? t({en:"▾ Hide detail",zh:"▾ 收起详情"}) : t({en:"▸ Open detail",zh:"▸ 展开详情"})}</Text>
    </Box>
    {expanded ? <Text color={error ? colors.danger : undefined} dimColor={!error}>
      {error || body || t({en:"Loading detail…",zh:"正在读取详情…"})}
    </Text> : null}
  </Box>;
}

export function RoundDetails({ message, client, project, verbose, width, includeDescendants = false, readTrace = true }: {
  message: ChatMessage; client?: BackendClient; project?: string;
  verbose: boolean; width: number; includeDescendants?: boolean; readTrace?: boolean;
}) {
  const t=useCopy();
  const cacheKey = traceCacheKey(project || "", message.trace_id || "", message.process_id || "", includeDescendants, message.status);
  const terminal = ["completed", "failed", "cancelled", "timed_out", "degraded"].includes(message.status || "");
  const [trace, setTrace] = useState<{ events: TraceRecord[]; truncated: boolean } | null>(
    () => client && terminal ? cachedTrace(client, cacheKey) : null);
  const [error, setError] = useState("");
  const canRead = Boolean(client && project && message.trace_id);
  useEffect(() => {
    if (!readTrace || !client || !project || !message.trace_id) return;
    let alive = true;
    const saved = terminal ? cachedTrace(client, cacheKey) : null;
    if (saved) {setTrace(saved); setError(""); return;}
    setTrace(null);
    setError("");
    void (async () => {
      // Deltas are token-granular. A fixed 100-event page can contain only a
      // fragment of reasoning and hide the tool/diff that follows it. Read the
      // completed turn in bounded batches, then collapse it into typed blocks.
      const pageSize = 5000;
      const maximumEvents = 50000;
      const events: TraceRecord[] = [];
      let afterSeq = 0;
      let truncated = false;
      while (events.length < maximumEvents) {
        const result: any = await runtimeTrace(client, project, {
          trace_id: message.trace_id, after_seq: afterSeq, limit: pageSize,
          process_id: includeDescendants ? "" : message.process_id,
          // Compact and verbose are projections of the same exact trace.
          include_deltas: true,
        });
        const batch = includeDescendants
          ? (result.events || [])
          : roundTraceEvents(result.events || [], message.process_id);
        events.push(...batch);
        const nextSeq = Number(result.next_seq || afterSeq);
        if (!result.has_more || nextSeq <= afterSeq) break;
        afterSeq = nextSeq;
        if (events.length >= maximumEvents) truncated = true;
      }
      const value = {events: events.slice(0, maximumEvents), truncated};
      if (terminal) cacheTrace(client, cacheKey, value);
      if (alive) setTrace(value);
    })().catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [client, project, message.trace_id, message.process_id, includeDescendants, readTrace, cacheKey, terminal]);

  const fallbackActivity: StreamActivity[] = message.activity?.length
    ? message.activity
    : [
        ...(message.reasoning ? [{kind: "reasoning" as const, text: message.reasoning}] : []),
        ...(message.tools || []).map((_tool, toolIndex) => ({kind: "tool" as const, toolIndex})),
        ...(message.notices || []).map((_notice, noticeIndex) => ({kind: "notice" as const, noticeIndex})),
      ];
  const fallback = {
    activity: fallbackActivity,
    tools: message.tools || [],
    notices: message.notices || [],
  };
  const model = trace ? timelineFromTrace(trace.events, message.process_id || "", {
    status: message.status,
    timestamp: message.timestamp,
  }) : {...fallback, providerUsage: message.provider_usage};

  if (!canRead || error) return <Box flexDirection="column">
    {error ? <Text color={colors.danger}>{t({en:"[TRACE_READ_FAILED] ",zh:"[TRACE_READ_FAILED] "})}{error}</Text> : null}
    <TurnTimeline {...fallback} verbose={verbose} width={width} preview/>
    {fallbackActivity.length === 0
      ? <Text dimColor>{t({en:"No saved trajectory is available.",zh:"没有已保存的轨迹。"})}</Text> : null}
  </Box>;

  return <Box flexDirection="column">
    {!trace && !error ? <Text dimColor>{t({en:"Loading trajectory…",zh:"正在读取轨迹…"})}</Text> : null}
    <TurnTimeline {...model} verbose={verbose} width={width} preview
      renderDetail={(detailRef, key) => <EventDetail key={key}
        detailRef={detailRef} client={client!} project={project!}/>}/>
    {trace && trace.events.length === 0
      ? <Text dimColor>{t({en:"No events are available for this agent; older traces may have expired.",zh:"此进程没有可读取事件，早期轨迹可能已过期。"})}</Text> : null}
    {trace?.truncated ? <Text color={colors.warning}>
      {t({en:"Trajectory exceeds 50,000 raw events; open /stats for the remaining trace.",zh:"轨迹超过 50,000 条原始事件，请通过 /stats 查看其余记录。"})}
    </Text> : null}
  </Box>;
}
