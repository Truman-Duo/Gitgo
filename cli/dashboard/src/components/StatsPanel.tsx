import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Box, ScrollBox, Text, type ScrollBoxHandle } from "@anthropic/ink";
import {
  useManagedInput as useInput,
  useScrollInput,
} from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { runtimeTrace, runtimeUsage } from "../backend/tools.js";
import {
  colors,
  formatDuration,
  truncate,
  padEndWidth,
} from "../theme/index.js";
import { waterfallRows, waterfallBar } from "./usagePresentation.js";

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  onDismiss: () => void;
};
export function StatsPanel({ client, project, cols, onDismiss }: Props) {
  const [refresh, setRefresh] = useState(0);
  const [tasks, setTasks] = useState<any[]>([]);
  const [selected, setSelected] = useState(0);
  const [events, setEvents] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [traceError, setTraceError] = useState("");
  const [loading, setLoading] = useState(true);
  const [traceLoading, setTraceLoading] = useState(false);
  const [page, setPage] = useState<any>({});
  const [tracePage, setTracePage] = useState<any>({});
  const generation = useRef(0);
  const scroll = useRef<ScrollBoxHandle>(null);
  useScrollInput(scroll);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    runtimeUsage(client, project, 50, "")
      .then((data: any) => {
        if (!alive) return;
        setTasks(data.tasks || []);
        setPage(data.page || {});
        setError("");
      })
      .catch((e) => {
        if (alive) setError(String(e.message || e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [client, project, refresh]);
  const task = tasks[selected];
  useEffect(() => {
    const seq = ++generation.current;
    setEvents([]);
    setTracePage({});
    setTraceError("");
    scroll.current?.scrollTo(0);
    if (!task) return;
    setTraceLoading(true);
    runtimeTrace(client, project, {
      action: "read",
      trace_id: task.task_id,
      limit: 1000,
      include_deltas: false,
    })
      .then((data: any) => {
        if (seq !== generation.current) return;
        setEvents(data.events || []);
        setTracePage(data);
      })
      .catch((e) => {
        if (seq === generation.current) setTraceError(String(e.message || e));
      })
      .finally(() => {
        if (seq === generation.current) setTraceLoading(false);
      });
    return () => {
      generation.current++;
    };
  }, [client, project, task?.task_id, refresh]);
  const loadMore = useCallback(() => {
    if (loading || traceLoading || !task) return;
    const seq = generation.current;
    if (tracePage.has_more) {
      setTraceLoading(true);
      runtimeTrace(client, project, {
        action: "read",
        trace_id: task.task_id,
        after_seq: tracePage.next_seq,
        limit: 1000,
        include_deltas: false,
      })
        .then((data: any) => {
          if (seq !== generation.current) return;
          setEvents((current) => {
            const seen = new Set(current.map((event) => event.seq));
            return [
              ...current,
              ...(data.events || []).filter(
                (event: any) => !seen.has(event.seq),
              ),
            ];
          });
          setTracePage(data);
          setTraceError("");
        })
        .catch((e) => {
          if (seq === generation.current) setTraceError(String(e.message || e));
        })
        .finally(() => {
          if (seq === generation.current) setTraceLoading(false);
        });
    } else if (page.has_more && page.next_cursor) {
      setLoading(true);
      runtimeUsage(client, project, 50, page.next_cursor)
        .then((data: any) => {
          if (seq !== generation.current) return;
          setTasks((current) => [...current, ...(data.tasks || [])]);
          setPage(data.page || {});
        })
        .catch((e) => setError(String(e.message || e)))
        .finally(() => setLoading(false));
    }
  }, [client, project, task, tracePage, page, traceLoading, loading]);
  useInput((input, key) => {
    if (key.escape) onDismiss();
    else if (key.leftArrow) setSelected((v) => Math.max(0, v - 1));
    else if (key.rightArrow)
      setSelected((v) => Math.min(tasks.length - 1, v + 1));
    else if (key.upArrow) scroll.current?.scrollBy(-1);
    else if (key.downArrow) scroll.current?.scrollBy(1);
    else if (input.toLowerCase() === "l") loadMore();
    else if (input.toLowerCase() === "r") {
      setSelected(0);
      setRefresh((v) => v + 1);
    }
  });
  const rows = useMemo(() => waterfallRows(events), [events]);
  const multipleProcesses = new Set(rows.map((row) => row.process)).size > 1;
  const showProcess = multipleProcesses && cols >= 88;
  const callWidth = cols < 72 ? 16 : 20;
  const tokenWidth = cols < 72 ? 13 : 19;
  const prefixWidth =
    5 + 1 + (showProcess ? 8 : 0) + callWidth + 1 + tokenWidth + 1 + 8 + 2;
  const chartWidth = Math.max(4, Math.min(28, cols - 2 - prefixWidth));
  const timed = rows.filter(
    (row) => row.start !== null && row.duration !== null,
  );
  const timelineMs = timed.length
    ? Math.max(...timed.map((row) => row.start! + row.duration!)) -
      Math.min(...timed.map((row) => row.start!))
    : null;
  const field = (value: string, width: number) =>
    padEndWidth(truncate(value, width), width);
  return (
    <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
      <Text bold>Stats · {project} · Turn waterfall</Text>
      {tasks.length ? (
        <Text>
          Turn {selected + 1} / {tasks.length}
          {page.has_more ? "+" : ""} · ← newer / → older
        </Text>
      ) : null}
      {error ? <Text color={colors.danger}>{error}</Text> : null}
      {loading ? (
        <Text dimColor>Loading task usage…</Text>
      ) : !task ? (
        <Text dimColor>No recorded task usage yet.</Text>
      ) : (
        <>
          <Text>
            {task.task_kind || "Task"} · {task.status} ·{" "}
            {formatDuration(Number(task.duration_ms || 0))} ·{" "}
            {Number(task.input_tokens || 0).toLocaleString()} in /{" "}
            {Number(task.output_tokens || 0).toLocaleString()} out ·{" "}
            {task.provider_calls} requests · {task.tool_calls} tools
          </Text>
          <Text dimColor>
            {String(task.created_at || "")
              .replace("T", " ")
              .slice(0, 19)}{" "}
            · {task.task_id}
          </Text>
        </>
      )}
      <ScrollBox
        ref={scroll}
        flexDirection="column"
        flexGrow={1}
        minHeight={0}
        marginTop={1}
      >
        {traceError ? (
          <Text color={colors.danger}>Trace unavailable: {traceError}</Text>
        ) : null}
        {traceLoading ? <Text dimColor>Loading recorded rounds…</Text> : null}
        {!rows.length && !traceLoading && task ? (
          <Text dimColor>
            No per-round timing retained for this task. Aggregate usage above
            remains available.
          </Text>
        ) : null}
        {rows.length ? (
          <>
            <Text dimColor>
              {field("Round", 5)} {showProcess ? field("Process", 7) + " " : ""}
              {field("Call", callWidth)} {field("Tokens in/out", tokenWidth)}{" "}
              {field("Time", 8)} Timeline
            </Text>
            <Text dimColor>
              {" ".repeat(prefixWidth)}
              {field("0", Math.max(1, chartWidth - 7))}
              {timelineMs === null ? "—" : formatDuration(timelineMs)}
            </Text>
          </>
        ) : null}
        {rows.map((row) => {
          const bar = waterfallBar(row, rows, chartWidth);
          const tokens =
            row.kind === "model"
              ? `${row.input === null ? "—" : row.input.toLocaleString()} / ${row.output === null ? "—" : row.output.toLocaleString()}`
              : "—";
          return (
            <Box key={row.id} flexDirection="column" flexShrink={0}>
              <Text wrap="truncate-end">
                <Text
                  color={
                    row.kind === "model"
                      ? colors.usage.model
                      : colors.usage.tool
                  }
                >
                  {field(row.kind === "model" ? `R${row.round}` : "  ↳", 5)}
                </Text>{" "}
                {showProcess ? (
                  <Text dimColor>{field(row.process.slice(0, 6), 7)} </Text>
                ) : null}
                {field(row.kind === "model" ? (!showProcess && multipleProcesses ? `Model ${row.process.slice(0, 6)}` : "Model") : row.label, callWidth)}{" "}
                <Text dimColor>{field(tokens, tokenWidth)}</Text>{" "}
                {field(
                  row.duration === null ? "—" : formatDuration(row.duration),
                  8,
                )}
                {"  "}
                <Text
                  color={
                    row.kind === "model"
                      ? colors.usage.model
                      : colors.usage.tool
                  }
                >
                  {bar
                    ? " ".repeat(bar.offset) +
                      (row.kind === "model" ? "▰" : "▪").repeat(bar.size)
                    : "—"}
                </Text>
              </Text>
              {row.status !== "complete" ? (
                <Text
                  color={
                    row.status.includes("failed") ||
                    row.status === "rolled back"
                      ? colors.danger
                      : colors.warning
                  }
                >
                  {" "}
                  {row.status}
                </Text>
              ) : null}
            </Box>
          );
        })}
      </ScrollBox>
      <Text dimColor>
        ▰ model · ▪ tool · — unrecorded · shared timeline per turn
      </Text>
      <Text dimColor>
        {events.length} retained events
        {tracePage.has_more ? " · partial trace; L loads more" : ""} ·{" "}
        {tasks.length} loaded tasks{page.has_more ? " · older available" : ""}
      </Text>
      <Text dimColor>
        ←/→ turn · ↑/↓ or PgUp/PgDn scroll · L load more · R refresh · Esc back
      </Text>
    </Box>
  );
}
