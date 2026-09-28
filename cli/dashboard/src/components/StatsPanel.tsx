import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { runtimeTrace, runtimeUsage } from "../backend/tools.js";
import { colors, formatDuration } from "../theme/index.js";
import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";

type Props = { client: BackendClient; project: string; cols: number; onDismiss: () => void };

const n = (value: unknown) => Number(value || 0).toLocaleString("en-US");
const pct = (read: unknown, input: unknown) => {
  const denominator = Number(input || 0);
  return denominator ? `${Math.round(Number(read || 0) / denominator * 100)}%` : "0%";
};

export function StatsPanel({ client, project, cols, onDismiss }: Props) {
  const [tasks, setTasks] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [eventSummary, setEventSummary] = useState<any>({ event_counts: {}, complete: false });
  const [nextCursor, setNextCursor] = useState("");
  const [hasOlder, setHasOlder] = useState(false);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState("");
  const [traceError, setTraceError] = useState("");
  const [traceLoading, setTraceLoading] = useState(false);
  const loadOlder = useCallback(() => {
    if (!hasOlder || !nextCursor || tasksLoading) return;
    setTasksLoading(true);
    runtimeUsage(client, project, 100, nextCursor)
      .then((value: any) => {
        const older = [...(value?.tasks || [])].reverse();
        setTasks((current) => {
          const known = new Set(current.map((item) => String(item.task_id)));
          const additions = older.filter((item) => !known.has(String(item.task_id)));
          setSelected(Math.max(0, additions.length - 1));
          return [...additions, ...current];
        });
        setNextCursor(String(value?.page?.next_cursor || ""));
        setHasOlder(Boolean(value?.page?.has_more));
      })
      .catch((e: any) => setError(String(e?.message || e)))
      .finally(() => setTasksLoading(false));
  }, [client, project, hasOlder, nextCursor, tasksLoading]);
  useInput((_input, key) => {
    if (key.escape) onDismiss();
    else if (key.leftArrow) {
      if (selected === 0 && hasOlder) loadOlder();
      else setSelected((v) => Math.max(0, v - 1));
    }
    else if (key.rightArrow) setSelected((v) => Math.min(Math.max(0, tasks.length - 1), v + 1));
  });
  useEffect(() => {
    let alive = true;
    setTasksLoading(true);
    runtimeUsage(client, project, 100, "")
      .then((value: any) => {
        if (!alive) return;
        const chronological = [...(value?.tasks || [])].reverse();
        setTasks(chronological);
        setSelected(Math.max(0, chronological.length - 1));
        setNextCursor(String(value?.page?.next_cursor || ""));
        setHasOlder(Boolean(value?.page?.has_more));
      })
      .catch((e: any) => { if (alive) setError(String(e?.message || e)); })
      .finally(() => { if (alive) setTasksLoading(false); });
    return () => { alive = false; };
  }, [client, project]);
  useEffect(() => {
    const task = tasks[selected];
    if (!task) { setEvents([]); setEventSummary({ event_counts: {}, complete: false }); return; }
    let alive = true;
    setTraceError("");
    setTraceLoading(true);
    setEvents([]);
    Promise.all([
      runtimeTrace(client, project, { action: "read", trace_id: task.task_id, limit: 500 }),
      runtimeTrace(client, project, { action: "summary", trace_id: task.task_id }),
    ])
      .then(([page, summary]: any[]) => {
        if (!alive) return;
        setEvents(page?.events || []);
        setEventSummary(summary || { event_counts: {}, complete: false });
      })
      .catch((error: any) => { if (alive) setTraceError(String(error?.message || error)); })
      .finally(() => { if (alive) setTraceLoading(false); });
    return () => { alive = false; };
  }, [client, project, tasks, selected]);
  const eventStats = useMemo(() => {
    let rejected = 0, compactions = 0;
    for (const [kind, count] of Object.entries(eventSummary?.event_counts || {})) {
      if (kind.includes("rejection") || kind.includes("blocked")) rejected += Number(count || 0);
      if (kind === "context_compaction_completed") compactions += Number(count || 0);
    }
    return { rejected, compactions };
  }, [eventSummary]);
  const task = tasks[selected] || {};
  const timelineItems = tasks.map((item, index) => {
    const status = item.status === "completed" ? "✓" : item.status === "failed" ? "×" : "·";
    return {id: String(item.task_id), label: `${status} ${index + 1} ${String(item.task_kind || "task").slice(0, 8)}`};
  });
  return (
    <Box flexDirection="column" paddingX={1} width={cols}>
      <Text bold>Stats · {project}</Text>
      <Text dimColor>←/→ task timeline · at left edge ← loads older tasks · Esc close</Text>
      {tasks.length > 0
        ? <HorizontalHeaderStrip items={timelineItems} selected={selected} width={Math.max(10, cols - 2)} />
        : <Text dimColor>No task usage yet</Text>}
      <Text color={colors.divider.color}>{"─".repeat(Math.max(10, cols - 2))}</Text>
      {error ? <Text color={colors.danger}>{error}</Text> : null}
      {traceError ? <Text color={colors.danger}>Trace unavailable: {traceError}</Text> : null}
      {tasks.length > 0 ? (
        <>
          <Text>Task ID           {String(task.task_id || "")}</Text>
          <Text>Process ID        {String(task.process_id || "")}</Text>
          <Text>Status            {task.status || "unknown"}</Text>
          <Text>Provider calls    {n(task.provider_calls)}</Text>
          <Text>Input tokens      {n(task.input_tokens)}</Text>
          <Text>Output tokens     {n(task.output_tokens)}</Text>
          <Text>Cache read        {n(task.cache_read_tokens)}  {pct(task.cache_read_tokens, task.input_tokens)}</Text>
          <Text>Tool calls        {n(task.tool_calls)}</Text>
          <Text>Duration          {formatDuration(Number(task.duration_ms || 0))}</Text>
          <Text>Loaded tasks      {tasks.length}{hasOlder ? " · older available" : " · complete"}{tasksLoading ? " · loading" : ""}</Text>
          <Text>Trace events      {traceError ? "unavailable" : traceLoading ? "loading" : eventSummary.complete ? `${n(eventSummary.event_count)} · exact` : `${events.length} · partial`}</Text>
          <Text>Governance stops  {traceError || traceLoading ? "—" : eventStats.rejected}</Text>
          <Text>Compactions       {traceError || traceLoading ? "—" : eventStats.compactions}</Text>
          {(eventSummary.recent_events || events.slice(-8)).map((event: any) => (
            <Text key={`${event.seq}-${event.event}`} dimColor>
              {String(event.time || "").slice(11, 19)}  {String(event.event || "event")}
            </Text>
          ))}
        </>
      ) : null}
    </Box>
  );
}
