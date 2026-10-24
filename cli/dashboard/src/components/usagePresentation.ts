/** Views over recorded usage/trace facts. Missing timing or billing stays unknown. */
export type WaterfallRow = {
  id: string;
  process: string;
  round: number;
  label: string;
  kind: "model" | "tool";
  start: number | null;
  duration: number | null;
  status: string;
  input: number | null;
  output: number | null;
};
const finite = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
function time(event: any): number | null {
  const n = Date.parse(String(event.time || event.timestamp || ""));
  return Number.isFinite(n) ? n : null;
}
export function waterfallRows(events: any[]): WaterfallRow[] {
  const rows: WaterfallRow[] = [];
  const latest = new Map<string, WaterfallRow>();
  for (const event of events) {
    const process = String(event.process_id || "main");
    const at = time(event);
    const kind = String(event.event || "");
    if (kind === "provider_request_started") {
      const row: WaterfallRow = {
        id: `model-${event.seq ?? rows.length}-${process}`,
        process,
        round: Number(event.step || (latest.get(process)?.round || 0) + 1),
        label: "Model request",
        kind: "model",
        start: at,
        duration: null,
        status: "no completion record",
        input: null,
        output: null,
      };
      rows.push(row);
      latest.set(process, row);
    } else if (
      [
        "provider_response_completed",
        "provider_response_incomplete",
        "provider_request_failed",
      ].includes(kind)
    ) {
      let row = latest.get(process);
      // A retained completion can outlive a truncated start; duration is still
      // usable when explicitly recorded, but no missing start is invented.
      if (!row || row.duration !== null) {
        row = {
          id: `model-${event.seq ?? rows.length}-${process}`,
          process,
          round: Number(event.completed_step || event.step || 0),
          label: "Model request",
          kind: "model",
          start: null,
          duration: null,
          status: "",
          input: null,
          output: null,
        };
        rows.push(row);
        latest.set(process, row);
      }
      row.duration =
        at !== null && row.start !== null
          ? Math.max(0, at - row.start)
          : finite(event.duration_ms);
      if (row.start === null && at !== null && row.duration !== null)
        row.start = at - row.duration;
      row.status =
        kind === "provider_response_completed"
          ? "complete"
          : kind === "provider_response_incomplete"
            ? "incomplete"
            : "failed";
    } else if (kind === "provider_usage") {
      const row = latest.get(process);
      if (!row) continue;
      const usage = event.usage || event;
      row.input = finite(usage.input_tokens);
      row.output = finite(usage.output_tokens);
    } else if (kind === "tool_executed" || kind === "tool_result") {
      const duration = finite(event.duration_ms);
      if (kind === "tool_result" && duration === null) continue;
      const id = `tool-${event.tool_call_id || event.call_id || event.seq || rows.length}-${process}`;
      if (rows.some((row) => row.id === id)) continue;
      rows.push({
        id,
        process,
        round: latest.get(process)?.round || Number(event.step || 0),
        label: String(event.tool_name || event.name || "Tool"),
        kind: "tool",
        start: at !== null && duration !== null ? at - duration : null,
        duration,
        status: event.rolled_back
          ? "rolled back"
          : event.allowed === false || event.is_error || event.error
            ? "blocked / failed"
            : "complete",
        input: null,
        output: null,
      });
    }
  }
  return rows;
}
export function waterfallBar(
  row: WaterfallRow,
  rows: WaterfallRow[],
  width: number,
): { offset: number; size: number } | null {
  if (row.start === null || row.duration === null) return null;
  const timed = rows.filter(
    (item) => item.start !== null && item.duration !== null,
  );
  const first = Math.min(...timed.map((item) => item.start!));
  const end = Math.max(...timed.map((item) => item.start! + item.duration!));
  const offset = Math.min(
    width - 1,
    Math.max(
      0,
      Math.floor(((row.start - first) / Math.max(1, end - first)) * width),
    ),
  );
  return {
    offset,
    size: Math.max(
      1,
      Math.min(
        width - offset,
        Math.round((row.duration / Math.max(1, end - first)) * width),
      ),
    ),
  };
}
export const tokenTotal = (value: any) =>
  Number(value?.input_tokens || 0) + Number(value?.output_tokens || 0);
export function localDay(date = new Date()): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
export function heatCalendar(
  daily: any[],
  end: string,
  weeks: number,
  metric: "tokens" | "requests" = "tokens",
) {
  const today = new Date(end + "T00:00:00Z");
  const weekday = (today.getUTCDay() + 6) % 7;
  const monday = today.getTime() - (weekday + (weeks - 1) * 7) * 86400000;
  const byDate = new Map(daily.map((day) => [day.date, day]));
  const visibleStart = new Date(monday).toISOString().slice(0, 10);
  const values = daily
    .filter((day) => day.date >= visibleStart && day.date <= end)
    .map((day) =>
      metric === "tokens" ? tokenTotal(day) : Number(day.provider_calls || 0),
    )
    .filter((n) => n > 0)
    .sort((a, b) => a - b);
  const thresholds = [0.25, 0.5, 0.75].map(
    (q) =>
      values[Math.min(values.length - 1, Math.floor(values.length * q))] ??
      Infinity,
  );
  return Array.from({ length: weeks }, (_, week) =>
    Array.from({ length: 7 }, (_, day) => {
      const key = new Date(monday + (week * 7 + day) * 86400000)
        .toISOString()
        .slice(0, 10);
      const usage = byDate.get(key);
      const value =
        metric === "tokens"
          ? tokenTotal(usage)
          : Number(usage?.provider_calls || 0);
      const level =
        value <= 0
          ? 0
          : value <= thresholds[0]!
            ? 1
            : value <= thresholds[1]!
              ? 2
              : value <= thresholds[2]!
                ? 3
                : 4;
      return { date: key, value, level, future: key > end, usage };
    }),
  );
}

export function moveCalendarCell(calendar:ReturnType<typeof heatCalendar>,day:string,
 direction:"left"|"right"|"up"|"down"):string {
 const column=calendar.findIndex(week=>week.some(cell=>cell.date===day));
 if(column<0)return day;
 const row=calendar[column]!.findIndex(cell=>cell.date===day);
 const nextColumn=column+(direction==="left"?-1:direction==="right"?1:0);
 const nextRow=row+(direction==="up"?-1:direction==="down"?1:0);
 if(nextColumn<0||nextColumn>=calendar.length||nextRow<0||nextRow>=7)return day;
 const cell=calendar[nextColumn]![nextRow]!;
 return cell.future?day:cell.date;
}
