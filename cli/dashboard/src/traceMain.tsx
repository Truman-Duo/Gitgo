import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { resolve } from "node:path";
import {
  AlternateScreen, Box, Text, renderSync, useApp, useInput, useTerminalSize,
} from "@anthropic/ink";
import { NativeHostClient } from "./backend/client.js";
import { resolvePythonRuntime } from "./backend/pythonRuntime.js";
import { colors, truncate, wrap } from "./theme/index.js";
import {
  eventSummary, mergeTraceEvents, shortPid, verboseEventLines,
  type TraceRecord,
} from "./traceView.js";

const GITGO_DIR = resolve(import.meta.dir, "../../..");
const PYTHON = resolvePythonRuntime();
const PROJECT = (() => {
  const index = process.argv.indexOf("--project");
  return index >= 0 ? String(process.argv[index + 1] || "gitgo") : "gitgo";
})();
const PINNED_TRACE = (() => {
  const index = process.argv.indexOf("--trace");
  return index >= 0 ? String(process.argv[index + 1] || "") : "";
})();

function TraceInspector({ client }: { client: NativeHostClient }) {
  const { exit } = useApp();
  const size = useTerminalSize();
  const rows = size.rows || process.stdout.rows || 30;
  const cols = size.columns || process.stdout.columns || 120;
  const [traceId, setTraceId] = useState(PINNED_TRACE);
  const [events, setEvents] = useState<TraceRecord[]>([]);
  const [selected, setSelected] = useState(-1);
  const [follow, setFollow] = useState(true);
  const [detail, setDetail] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<"timeline" | "verbose">("verbose");
  const eventsRef = useRef<TraceRecord[]>([]);
  const traceIdRef = useRef(PINNED_TRACE);
  const followRef = useRef(true);
  const refreshInFlight = useRef(false);

  useEffect(() => { followRef.current = follow; }, [follow]);

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    try {
      let active = traceIdRef.current;
      if (!PINNED_TRACE) {
        const listing = await client.callTool("runtime.trace", {
          project: PROJECT, action: "list",
        }, 10);
        const latest = String(listing?.traces?.[0]?.trace_id || "");
        if (latest && latest !== active) {
          active = latest;
          traceIdRef.current = latest;
          eventsRef.current = [];
          setEvents([]);
          setSelected(-1);
          setDetail(null);
          setTraceId(latest);
        }
      }
      if (!active) return;
      const current = eventsRef.current;
      const after = current.length > 0 ? current[current.length - 1].seq : 0;
      const page = await client.callTool("runtime.trace", {
        project: PROJECT, action: "read", trace_id: active,
        after_seq: after, limit: 2000,
      }, 10);
      const incoming = (page?.events || []) as TraceRecord[];
      if (incoming.length > 0) {
        const next = mergeTraceEvents(current, incoming);
        eventsRef.current = next;
        setEvents(next);
        if (followRef.current) setSelected(Math.max(0, next.length - 1));
      }
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      refreshInFlight.current = false;
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 350);
    return () => clearInterval(timer);
  }, [refresh]);

  const loadDetail = useCallback(async () => {
    const item = events[selected];
    if (!item?.detail_ref) {
      setDetail(item || null);
      return;
    }
    try {
      const result = await client.callTool("runtime.trace", {
        project: PROJECT, action: "detail", ref: item.detail_ref,
      }, 10);
      setDetail(result?.detail ?? result);
    } catch (reason) {
      setDetail({ error: reason instanceof Error ? reason.message : String(reason) });
    }
  }, [client, events, selected]);

  useInput((input: string, key: any) => {
    if (input === "q" || (key.ctrl && input === "c")) exit();
    else if (input === "f" || input === " ") setFollow(value => !value);
    else if (input === "v") setView(value => value === "verbose" ? "timeline" : "verbose");
    else if (input === "r") void refresh();
    else if (key.upArrow || input === "k") {
      setFollow(false);
      setSelected(value => Math.max(0, value - 1));
      setDetail(null);
    } else if (key.downArrow || input === "j") {
      setSelected(value => Math.min(events.length - 1, value + 1));
      setDetail(null);
    } else if (key.return || input === "d") void loadDetail();
  });

  const selectedIndex = selected < 0 ? Math.max(0, events.length - 1) : selected;
  const bodyRows = Math.max(6, rows - 5);
  const start = Math.max(0, selectedIndex - Math.floor(bodyRows / 2));
  const visible = events.slice(start, start + bodyRows);
  const listWidth = cols >= 110 ? Math.floor(cols * 0.58) : cols;
  const selectedEvent = events[selectedIndex];
  const detailValue = detail ?? selectedEvent ?? {};
  const detailText = useMemo(
    () => JSON.stringify(detailValue, null, 2),
    [detailValue],
  );
  const detailLines = wrap(detailText, Math.max(20, cols - listWidth - 4)).slice(0, bodyRows);
  const verboseLines = useMemo(() => events.flatMap(
    event => verboseEventLines(event, cols - 1),
  ), [cols, events]);
  const visibleVerbose = follow
    ? verboseLines.slice(-bodyRows)
    : verboseLines.slice(Math.max(0, verboseLines.length - bodyRows));

  return (
    <Box flexDirection="column" width={cols} height={rows}>
      <Box justifyContent="space-between">
        <Text color={colors.accent} bold>Gitgo Trace Inspector</Text>
        <Text dimColor>{PROJECT} · {traceId || "waiting for trace"}</Text>
      </Box>
      <Text dimColor>q quit · v timeline/verbose · j/k move · d detail · f follow · r refresh</Text>
      {view === "verbose" ? (
        <Box flexDirection="column" flexGrow={1}>
          {visibleVerbose.map((line, index) => (
            <Text key={`${verboseLines.length - visibleVerbose.length + index}-${line.slice(0, 24)}`}>{line}</Text>
          ))}
        </Box>
      ) : <Box flexDirection="row" flexGrow={1}>
        <Box flexDirection="column" width={listWidth}>
          {visible.map((event, offset) => {
            const index = start + offset;
            const active = index === selectedIndex;
            const time = String(event.time || "").slice(11, 23);
            const line = `${String(event.seq).padStart(5)} ${time} ${shortPid(event.process_id)} ${eventSummary(event)}`;
            return <Text key={`${traceId}-${event.seq}`} inverse={active}>{truncate(line, listWidth - 1)}</Text>;
          })}
        </Box>
        {cols >= 110 ? (
          <Box flexDirection="column" width={cols - listWidth} paddingLeft={1}>
            <Text bold>{selectedEvent?.detail_ref ? "Event (d to load detail)" : "Event"}</Text>
            {detailLines.map((line, index) => <Text key={index} dimColor>{line}</Text>)}
          </Box>
        ) : null}
      </Box>}
      <Text color={error ? colors.danger : undefined} dimColor={!error}>
        {error || `${events.length} events · ${view} · ${PINNED_TRACE ? "pinned" : "auto-follow latest trace"} · ${follow ? "following live tail" : "inspection paused"}`}
      </Text>
    </Box>
  );
}

async function main() {
  const client = new NativeHostClient(PYTHON, GITGO_DIR);
  await client.start();
  const { waitUntilExit } = renderSync(
    <AlternateScreen mouseTracking={false}>
      <TraceInspector client={client} />
    </AlternateScreen>,
    { exitOnCtrlC: false },
  );
  await waitUntilExit();
  await client.close();
  process.exit(0);
}

main().catch(error => {
  console.error("Trace Inspector error:", error);
  process.exit(1);
});
