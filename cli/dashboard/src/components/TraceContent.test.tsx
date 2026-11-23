import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, Text, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import type {TraceBodyRef, TraceBodyReader} from "../chat/traceSource.js";
import {TraceContent} from "./TraceContent.js";
const wait = () => new Promise(resolve => setTimeout(resolve, 60));
const text = (node: any): string => node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
function mount(element: React.ReactNode) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: false, columns: 90, rows: 30}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const root = renderSync(<Box width={90}>{element}</Box>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {rerender(value: React.ReactNode) {root.rerender(<Box width={90}>{value}</Box>);},
    text: () => text((instances.get(stdout) as any).rootNode),
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}};
}
const ref: TraceBodyRef = {schema: 1, snapshot: {schema: 1, trace_id: "turn", process_id: "A", first_seq: 1, last_seq: 1, event_count: 1},
  first_seq: 1, last_seq: 1, event: "reasoning_delta", process_id: "A", event_count: 1, text_chars: 1};
const draw = (read: TraceBodyReader) => <TraceContent read={read} references={[ref]}>
  {events => <Text>{events?.map(event => event.delta).join("") || "HEADER"}</Text>}
</TraceContent>;
test("changing the read owner discards old content and late results even with identical trace locations", async () => {
  let resolveOld: (events: any[]) => void = () => {};
  let oldSignal: AbortSignal | undefined;
  const oldRead: TraceBodyReader = (_refs, signal) => {
    oldSignal = signal; return new Promise(resolve => {resolveOld = resolve;});
  };
  const newRead: TraceBodyReader = async () => [{seq: 1, time: "now", event: "reasoning_delta", delta: "NEW PROJECT BODY"}];
  const io = mount(draw(oldRead));
  try {
    await wait(); io.rerender(draw(newRead)); await wait();
    expect(oldSignal?.aborted).toBe(true);
    resolveOld([{seq: 1, event: "reasoning_delta", delta: "STALE PROJECT BODY"}]); await wait();
    expect(io.text()).toContain("NEW PROJECT BODY");
    expect(io.text()).not.toContain("STALE PROJECT BODY");
  } finally {io.close();}
});
test("closing a record cancels its body lifetime and reopening performs a fresh read", async () => {
  const signals: AbortSignal[] = [];
  const read: TraceBodyReader = (_refs, signal) => {signals.push(signal); return new Promise(() => {});};
  const io = mount(draw(read));
  try {
    await wait(); io.rerender(null); await wait();
    expect(signals[0]?.aborted).toBe(true);
    io.rerender(draw(read)); await wait(); expect(signals.length).toBe(2);
  } finally {io.close();}
  expect(signals[1]?.aborted).toBe(true);
});
