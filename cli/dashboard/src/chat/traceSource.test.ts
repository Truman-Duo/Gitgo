import {expect, test} from "bun:test";
import {readTraceIndex, traceBodyReader, type TraceBodyRef, type TraceSnapshot} from "./traceSource.js";
import {timelineFromTrace} from "./turnTimeline.js";

const snapshot: TraceSnapshot = {schema: 1, trace_id: "turn", process_id: "A", first_seq: 1, last_seq: 2, event_count: 2};
const ref: TraceBodyRef = {schema: 1, snapshot, first_seq: 1, last_seq: 2, event: "reasoning_delta",
  process_id: "A", event_count: 2, text_chars: 8};
const body = [
  {seq: 1, time: "now", event: "reasoning_delta", process_id: "A", delta: "中文👨‍👩‍👧‍👦"},
  {seq: 2, time: "now", event: "reasoning_delta", process_id: "A", delta: "e\u0301尾"},
];
test("the body reader follows bounded source cursors automatically and preserves exact Unicode", async () => {
  const requests: any[] = [];
  const client: any = {callTool: async (_: string, args: any) => {
    requests.push(args);
    return {snapshot, events: [body[args.after_seq]], next_seq: args.after_seq + 1, has_more: args.after_seq === 0};
  }};
  const read = traceBodyReader(client, "project", "turn");
  const result = await read([ref], new AbortController().signal);
  expect(result).toEqual(body);
  expect(requests.map(item => item.after_seq)).toEqual([0, 1]);
  expect(requests.every(item => item.action === "block" && item.limit === 500)).toBe(true);
  await read([ref], new AbortController().signal);
  expect(requests.length).toBe(2);
});
test("an exited record never requests a later body page", async () => {
  const controller = new AbortController(); let calls = 0;
  const client: any = {callTool: async () => {
    calls++; controller.abort();
    return {snapshot, events: [body[0]], next_seq: 1, has_more: true};
  }};
  await expect(traceBodyReader(client, "project", "turn")([ref], controller.signal)).rejects.toThrow("cancelled");
  expect(calls).toBe(1);
});
test("wrong process, snapshot or nonadvancing body cursor cannot become visible content", async () => {
  for (const page of [
    {snapshot, events: [{...body[0], process_id: "B"}], next_seq: 1, has_more: true},
    {snapshot: {...snapshot, last_seq: 3}, events: [body[0]], next_seq: 1, has_more: true},
    {snapshot, events: [], next_seq: 0, has_more: true},
  ]) {
    const client: any = {callTool: async () => page};
    await expect(traceBodyReader(client, "project", "turn")([ref], new AbortController().signal)).rejects.toThrow();
  }
});
test("summary precedes usable index pages and the snapshot pins subsequent reads", async () => {
  const calls: any[] = [], order: string[] = [];
  const metadata = {seq: 1, event: "reasoning_delta", process_id: "A", body_ref: ref};
  const lastRef = {...ref, first_seq: 3, last_seq: 3, event: "tool_result", event_count: 1,
    snapshot: {...snapshot, last_seq: 3, event_count: 3}};
  const frozen = lastRef.snapshot;
  const client: any = {callTool: async (_: string, args: any) => {
    calls.push(args);
    if (args.action === "summary") return {trace_id: "turn", event_count: 3};
    return args.after_seq === -1
      ? {snapshot: frozen, events: [{...metadata, body_ref: {...ref, snapshot: frozen}}], next_seq: 2, has_more: true}
      : {snapshot: frozen, events: [{seq: 3, event: "tool_result", process_id: "A", tool_name: "read_file", body_ref: lastRef}], next_seq: 3, has_more: false};
  }};
  await readTraceIndex(client, "project", "turn", "A", new AbortController().signal,
    () => {order.push("summary");}, (events, complete) => {order.push(complete ? "complete" : "prefix"); expect(events.length).toBe(complete ? 2 : 1);});
  expect(order).toEqual(["summary", "prefix", "complete"]);
  expect(calls[0].include_recent).toBe(false);
  expect(calls[2].snapshot).toEqual(frozen);
});
test("index projection retains body locations without inventing text or promoting final drafts", () => {
  const events: any[] = [
    {seq: 1, event: "reasoning_delta", process_id: "A", body_ref: ref},
    {seq: 3, event: "text_delta", process_id: "A", visibility: "verbose", body_ref: {...ref, event: "text_delta", first_seq: 3, last_seq: 3}},
    {seq: 4, event: "toolcall_start", process_id: "A", tool_call_id: "tool", tool_name: "read_file"},
    {seq: 5, event: "progress_delta", process_id: "A", update_id: "update", task_id: "task", body_ref: {...ref, event: "progress_delta", first_seq: 5}},
    {seq: 6, event: "progress_summary", process_id: "A", update_id: "update", task_id: "task", message: "metadata preview", body_ref: {...ref, event: "progress_summary", first_seq: 6}},
    {seq: 7, event: "text_delta", process_id: "A", visibility: "public", body_ref: {...ref, event: "text_delta", first_seq: 7}},
    {seq: 8, event: "agent_terminal", process_id: "A"},
  ];
  const model = timelineFromTrace(events, "A");
  expect(model.activity.map(item => item.kind)).toEqual(["reasoning", "progress", "tool", "progress", "notice"]);
  expect(model.activity[0]!.trace_refs).toEqual([ref]);
  const privateText = model.activity[1]!;
  expect("text" in privateText && privateText.text).toBe("");
  expect("visibility" in privateText && privateText.visibility).toBe("verbose");
  expect(model.activity.find(item => item.kind === "progress" && item.update_id === "update")!.trace_refs?.map(item => item.first_seq)).toEqual([6]);
});
