import React, { useEffect, useRef, useState } from "react";
import { Box, ScrollBox, Text, type ScrollBoxHandle } from "@anthropic/ink";
import {
  useManagedInput as useInput,
  useScrollInput,
} from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { runtimeUsage } from "../backend/tools.js";
import {
  colors,
  truncate,
  padEndWidth,
  formatDuration,
} from "../theme/index.js";
import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";
import { heatCalendar, localDay, tokenTotal } from "./usagePresentation.js";
type Props = {
  client: BackendClient;
  cols: number;
  onDismiss: () => void;
  onInspect?: (project: string) => void;
};
const palette = colors.usage.heat;
export function StatsOverview({ client, cols, onDismiss, onInspect }: Props) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState(0);
  const [selected, setSelected] = useState(0);
  const [day, setDay] = useState(localDay());
  const [metric, setMetric] = useState<"tokens" | "requests">("tokens");
  const [refresh, setRefresh] = useState(0);
  const scroll = useRef<ScrollBoxHandle>(null);
  useScrollInput(scroll);
  useEffect(() => {
    let alive = true;
    setError("");
    runtimeUsage(client)
      .then((value) => {
        if (alive) setData(value);
      })
      .catch((e) => {
        if (alive) setError(String(e.message || e));
      });
    return () => {
      alive = false;
    };
  }, [client, refresh]);
  const tasks = data?.tasks || [];
  const projects = data?.projects || [];
  const list = view === 1 ? tasks : projects;
  const weeks = Math.max(4, Math.min(53, Math.floor((cols - 12) / 2)));
  const calendar = heatCalendar(data?.daily || [], localDay(), weeks, metric);
  useInput((input, key) => {
    if (key.escape) onDismiss();
    else if (key.tab) {
      setView((v) => (v + 1) % 3);
      setSelected(0);
      scroll.current?.scrollTo(0);
    } else if (input.toLowerCase() === "m")
      setMetric((m) => (m === "tokens" ? "requests" : "tokens"));
    else if (input.toLowerCase() === "r") setRefresh((n) => n + 1);
    else if (view === 0 && (key.leftArrow || key.rightArrow)) {
      const dt = new Date(day + "T00:00:00Z");
      dt.setUTCDate(dt.getUTCDate() + (key.leftArrow ? -1 : 1));
      const next = dt.toISOString().slice(0, 10);
      if (next >= calendar[0]![0]!.date && next <= localDay()) setDay(next);
    } else if (key.upArrow) setSelected((v) => Math.max(0, v - 1));
    else if (key.downArrow) {
      setSelected((v) => Math.min(list.length - 1, v + 1));
      scroll.current?.scrollBy(1);
    } else if (key.return && view !== 0) {
      const project =
        view === 1 ? list[selected]?.project : list[selected]?.project;
      if (project) onInspect?.(project);
    }
  });
  const summary = data?.summary || {};
  const selectedDay = (data?.daily || []).find(
    (value: any) => value.date === day,
  );
  return (
    <Box flexDirection="column" paddingX={1} flexGrow={1} minHeight={0}>
      <Text bold>Usage overview</Text>
      <HorizontalHeaderStrip
        items={[
          { id: "daily", label: "Daily activity" },
          { id: "tasks", label: "Recent tasks" },
          { id: "projects", label: "Projects" },
        ]}
        selected={view}
        width={cols - 4}
      />
      {error ? (
        <Text color={colors.danger}>{error}</Text>
      ) : !data ? (
        <Text dimColor>Loading authoritative usage…</Text>
      ) : (
        <>
          <Text>
            {summary.task_count} tasks · {summary.provider_calls} requests ·{" "}
            {tokenTotal(summary).toLocaleString()} tokens · cache reads{" "}
            {Number(summary.cache_read_tokens || 0).toLocaleString()}
          </Text>
          {data.complete === false ? (
            <Text color={colors.warning}>
              Partial totals: {projects.filter((p: any) => p.error).length}{" "}
              project(s) unavailable.
            </Text>
          ) : null}
        </>
      )}
      <ScrollBox
        ref={scroll}
        flexDirection="column"
        flexGrow={1}
        minHeight={0}
        marginTop={1}
      >
        {!data ? (
          <Text dimColor>
            Daily usage will appear after the query completes.
          </Text>
        ) : view === 0 ? (
          <>
            <Text bold>
              {metric === "tokens"
                ? "Reported input + output tokens"
                : "Provider requests"}{" "}
              · last {weeks} weeks
            </Text>
            <Text dimColor>
              {calendar[0]![0]!.date} — {localDay()} · UTC
              {Number(data?.day_offset_minutes || 0) >= 0 ? "+" : ""}
              {Number(data?.day_offset_minutes || 0) / 60}
            </Text>
            {Array.from({ length: 7 }, (_, weekday) => (
              <Text key={weekday}>
                <Text dimColor>
                  {["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"][weekday]}{" "}
                </Text>
                {calendar.map((week, index) => {
                  const cell = week[weekday]!;
                  return (
                    <Text
                      key={index}
                      color={
                        cell.date === day
                          ? colors.selection.block.fg
                          : palette[cell.level]
                      }
                      backgroundColor={
                        cell.date === day
                          ? colors.selection.block.bg
                          : undefined
                      }
                    >
                      {cell.future ? "  " : "■ "}
                    </Text>
                  );
                })}
              </Text>
            ))}
            <Text dimColor>
              Less{" "}
              {palette.map((color, i) => (
                <Text color={color} key={i}>
                  ■{" "}
                </Text>
              ))}{" "}
              More · nonzero-day quartiles
            </Text>
            <Text>
              {day} · {tokenTotal(selectedDay).toLocaleString()} tokens ·{" "}
              {Number(selectedDay?.provider_calls || 0)} requests ·{" "}
              {Number(selectedDay?.task_count || 0)} tasks
            </Text>
            <Text dimColor>
              Tasks are assigned to their start date. Cache reads are a separate
              metric, not added again.
            </Text>
            <Text dimColor>
              USD cost is unrecorded; no billing estimate is fabricated.
              {data?.undated_tasks
                ? ` ${data.undated_tasks} task(s) have no valid date.`
                : ""}
            </Text>
          </>
        ) : view === 1 ? (
          <>
            <Text dimColor>
              Most recent {tasks.length} tasks · Enter opens the project
              waterfall
            </Text>
            {tasks.map((task: any, index: number) => (
              <Text
                key={task.project + task.task_id}
                backgroundColor={
                  index === selected ? colors.selection.row.bg : undefined
                }
              >
                {padEndWidth(truncate(task.project, 18), 18)}{" "}
                {String(task.created_at || "").slice(0, 10)}{" "}
                {padEndWidth(truncate(task.task_kind || "task", 10), 10)}{" "}
                {String(tokenTotal(task).toLocaleString()).padStart(10)} tokens{" "}
                {formatDuration(Number(task.duration_ms || 0))} · {task.status}
              </Text>
            ))}
          </>
        ) : (
          projects.map((project: any, index: number) => (
            <Text
              key={project.project}
              backgroundColor={
                index === selected ? colors.selection.row.bg : undefined
              }
            >
              {padEndWidth(truncate(project.project, 22), 22)}{" "}
              {project.error
                ? `Unavailable: ${project.error}`
                : project.alias_of
                  ? `Shared data with ${project.alias_of}; counted once`
                  : `${project.task_count} tasks · ${tokenTotal(project).toLocaleString()} tokens · ${project.provider_calls} requests`}
            </Text>
          ))
        )}
      </ScrollBox>
      <Text dimColor>
        Tab view · ←/→ day · ↑/↓ select · Enter project · M metric · R refresh ·
        Esc back
      </Text>
    </Box>
  );
}
