import {label} from "../ui/runtimeLabels.js";
import {PageFrame,useCopy} from "../ui/PageFrame.js";
import {pages,standardKey,moveSelection,reconcileSelection} from "../ui/pageContract.js";
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
  const t=useCopy();
  const [actionFocus,setActionFocus]=useState(false),[action,setAction]=useState(0);
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
  useScrollInput(scroll,!actionFocus);
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
  const actions=[{id:0,enabled:!loading&&!traceLoading&&Boolean(tracePage.has_more||page.has_more)},{id:1,enabled:!loading&&!traceLoading}];
  const activeAction=reconcileSelection(actions,action);
  useInput((input,key)=>{
    const command=standardKey(input,key);
    if(command==="back"){if(actionFocus)setActionFocus(false);else onDismiss();return;}
    if(command==="next"){setActionFocus(v=>!v);return;}
    if(actionFocus){
      if(command==="left"||command==="right")setAction(v=>moveSelection(actions,v,command==="left"?-1:1)??v);
      else if(command==="up")setActionFocus(false);
      else if(command==="activate"){
        if(activeAction===0)loadMore();
        else if(activeAction===1){setSelected(0);setRefresh(v=>v+1);}
      }
      return;
    }
    if(command==="left")setSelected(v=>Math.max(0,v-1));
    else if(command==="right")setSelected(v=>Math.min(tasks.length-1,v+1));
    else if(command==="up")scroll.current?.scrollBy(-1);
    else if(command==="down")scroll.current?.scrollBy(1);
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
    <PageFrame page={pages.trace}>
      <Text bold>{t({en:"Stats",zh:"用量"})} · {project} · {t({en:"Turn waterfall",zh:"逐轮瀑布图"})}</Text>
      {tasks.length ? (
        <Text>
          {t({en:"Turn",zh:"轮次"})} {selected + 1} / {tasks.length}
          {page.has_more ? "+" : ""} · {t({en:"← newer / → older",zh:"← 较新 / → 较旧"})}
        </Text>
      ) : null}
      {error ? <Text color={colors.danger}>{error}</Text> : null}
      {loading ? (
        <Text dimColor>{t({en:"Loading task usage…",zh:"正在读取任务用量…"})}</Text>
      ) : !task ? (
        <Text dimColor>{t({en:"No recorded task usage yet.",zh:"尚无任务用量记录。"})}</Text>
      ) : (
        <>
          <Text>
            {label(task.task_kind || "Task",t)} · {label(task.status,t)} ·{" "}
            {formatDuration(Number(task.duration_ms || 0))} ·{" "}
            {Number(task.input_tokens || 0).toLocaleString()} {t({en:"in",zh:"输入"})} /{" "}
            {Number(task.output_tokens || 0).toLocaleString()} {t({en:"out",zh:"输出"})} ·{" "}
            {task.provider_calls} {t({en:"requests",zh:"次请求"})} · {task.tool_calls} {t({en:"tools",zh:"次工具调用"})}
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
          <Text color={colors.danger}>{t({en:"Trace unavailable: ",zh:"执行记录不可用："})}{traceError}</Text>
        ) : null}
        {traceLoading ? <Text dimColor>{t({en:"Loading recorded rounds…",zh:"正在读取各轮记录…"})}</Text> : null}
        {!rows.length && !traceLoading && task ? (
          <Text dimColor>
            {t({en:"No per-round timing retained for this task. Aggregate usage above remains available.",zh:"此任务未保留逐轮时间记录，仍可查看上方汇总用量。"})}
          </Text>
        ) : null}
        {rows.length ? (
          <>
            <Text dimColor>
              {field(t({en:"Round",zh:"轮次"}), 5)} {showProcess ? field(t({en:"Process",zh:"进程"}), 7) + " " : ""}
              {field(t({en:"Call",zh:"调用"}), callWidth)} {field(t({en:"Tokens in/out",zh:"输入/输出词元"}), tokenWidth)}{" "}
              {field(t({en:"Time",zh:"用时"}), 8)} {t({en:"Timeline",zh:"时间线"})}
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
                {field(row.kind === "model" ? t({en:"Model",zh:"模型"}) + (!showProcess && multipleProcesses ? " "+row.process.slice(0,6) : "") : row.label, callWidth)}{" "}
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
                  {label(row.status,t)}
                </Text>
              ) : null}
            </Box>
          );
        })}
      </ScrollBox>
      <Text dimColor>{t({en:"▰ model · ▪ tool · — unrecorded · shared timeline per turn",zh:"▰ 模型 · ▪ 工具 · — 未记录 · 同轮共用时间轴"})}</Text>
      <Text dimColor>{events.length} {t({en:"retained events",zh:"条保留事件"})}{tracePage.has_more?t({en:" · partial trace",zh:" · 执行记录尚未载全"}):""} · {tasks.length} {t({en:"loaded tasks",zh:"项已载入任务"})}{page.has_more?t({en:" · older tasks available",zh:" · 还有更早任务"}):""}</Text>
      <Box gap={2}>{[t({en:"Load more",zh:"加载更多"}),t({en:"Refresh",zh:"刷新"})].map((label,index)=><Text key={index}
        dimColor={!actions[index]?.enabled}
        color={actionFocus&&activeAction===index?colors.selection.block.fg:undefined}
        backgroundColor={actionFocus&&activeAction===index?colors.selection.block.bg:undefined}>{label}</Text>)}</Box>
      <Text dimColor>{actionFocus?t({en:"←/→ actions · Enter apply · Tab chart · Esc chart",zh:"←/→ 操作 · Enter 执行 · Tab 图表 · Esc 图表"}):t({en:"←/→ turn · ↑/↓ or PgUp/PgDn scroll · Tab actions · Esc back",zh:"←/→ 轮次 · ↑/↓ 或 PgUp/PgDn 滚动 · Tab 操作 · Esc 返回"})}</Text>
    </PageFrame>
  );
}
