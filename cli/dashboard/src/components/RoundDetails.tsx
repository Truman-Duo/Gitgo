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
import { timelineFromTrace } from "../chat/turnTimeline.js";
import { TurnTimeline } from "./TurnTimeline.js";

function EventDetail({ detailRef, client, project }: {
  detailRef: string; client: BackendClient; project: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!expanded || body !== null || !detailRef) return;
    let alive = true;
    setError("");
    void runtimeTrace(client, project, { action: "detail", ref: detailRef })
      .then((result: any) => {
        if (alive) setBody(JSON.stringify(result.detail, null, 2) ?? "No saved detail");
      })
      .catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [expanded, body, detailRef, client, project]);
  return <Box flexDirection="column" paddingLeft={2}>
    <Box onClick={() => setExpanded(value => !value)}>
      <Text dimColor>{expanded ? "▾ Hide detail" : "▸ Open detail"}</Text>
    </Box>
    {expanded ? <Text color={error ? colors.danger : undefined} dimColor={!error}>
      {error || body || "Loading detail…"}
    </Text> : null}
  </Box>;
}

export function RoundDetails({ message, client, project, verbose, width, includeDescendants = false }: {
  message: ChatMessage; client?: BackendClient; project?: string;
  verbose: boolean; width: number; includeDescendants?: boolean;
}) {
  const [trace, setTrace] = useState<{ events: TraceRecord[]; truncated: boolean } | null>(null);
  const [error, setError] = useState("");
  const canRead = Boolean(client && project && message.trace_id);
  useEffect(() => {
    if (!client || !project || !message.trace_id) return;
    let alive = true;
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
      if (alive) setTrace({ events: events.slice(0, maximumEvents), truncated });
    })().catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [client, project, message.trace_id, message.process_id, includeDescendants]);

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

  if (!canRead || error) return <Box flexDirection="column" paddingLeft={2}>
    {error ? <Text color={colors.danger}>[TRACE_READ_FAILED] {error}</Text> : null}
    <TurnTimeline {...fallback} verbose={verbose} width={width}/>
    {fallbackActivity.length === 0
      ? <Text dimColor>No saved trajectory is available.</Text> : null}
  </Box>;

  return <Box flexDirection="column" paddingLeft={2}>
    {!trace && !error ? <Text dimColor>Loading trajectory…</Text> : null}
    <TurnTimeline {...model} verbose={verbose} width={width}
      renderDetail={(detailRef, key) => <EventDetail key={key}
        detailRef={detailRef} client={client!} project={project!}/>}/>
    {trace && trace.events.length === 0
      ? <Text dimColor>No events are available for this agent; older traces may have expired.</Text> : null}
    {trace?.truncated ? <Text color={colors.warning}>
      Trajectory exceeds 50,000 raw events; open /stats for the remaining trace.
    </Text> : null}
  </Box>;
}
