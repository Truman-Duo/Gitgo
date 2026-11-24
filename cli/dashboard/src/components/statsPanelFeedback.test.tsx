import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {StatsPanel} from "./StatsPanel.js";
import {colors} from "../theme/index.js";

const wait = (ms = 90) => new Promise(resolve => setTimeout(resolve, ms));
const task = (id: string) => ({task_id: id, status: "completed", provider_calls: 0, tool_calls: 0});
function mount(query: (name: string, args: any) => any) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 110, rows: 24}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const calls: {name: string; args: any}[] = [];
  const client: any = {async callTool(name: string, args: any) {calls.push({name, args}); return query(name, args);}};
  const root = renderSync(<InputProvider><Box width={110} height={24} flexDirection="column">
    <StatsPanel client={client} project="demo" cols={110} onDismiss={() => {}}/>
  </Box></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {calls,
    text() {const screen = (instances.get(stdout) as any).frontFrame.screen; return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? "").join("")).join("\n");},
    async send(value: string) {stdin.write(value); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();},
  };
}

test("task usage error expires without presenting a failed read as an empty history, and refresh can recover", async () => {
  const previous = colors.feedback.errorMs; Object.assign(colors.feedback, {errorMs: 180});
  let reads = 0;
  const io = mount(name => {
    if (name === "runtime.trace") return {events: [], has_more: false};
    if (reads++ === 0) throw Error("Task usage read failed");
    return {tasks: [task("recovered-task")], page: {}};
  });
  try {
    await wait(); expect(io.text()).toContain("Task usage read failed");
    expect(io.text()).not.toContain("No recorded task usage yet");
    await wait(200); expect(io.text()).not.toContain("Task usage read failed");
    expect(io.text()).toContain("Task usage not loaded");
    await io.send("\t"); await io.send("\r");
    expect(io.text()).toContain("recovered-task"); expect(io.text()).not.toContain("Task usage not loaded");
    expect(io.calls.filter(call => call.name === "runtime.usage")).toHaveLength(2);
  } finally {io.close(); Object.assign(colors.feedback, {errorMs: previous});}
});

test("trace error expires and stays with the selected task", async () => {
  const previous = colors.feedback.errorMs; Object.assign(colors.feedback, {errorMs: 180});
  const io = mount((name, args) => {
    if (name === "runtime.usage") return {tasks: [task("task-a"), task("task-b")], page: {}};
    if (args.trace_id === "task-a") throw Error("task-a trace read failed");
    return {events: [], has_more: false};
  });
  try {
    await wait(); expect(io.text()).toContain("task-a trace read failed");
    await wait(200); expect(io.text()).not.toContain("task-a trace read failed");
    expect(io.text()).toContain("Round records not loaded");
    expect(io.text()).not.toContain("No per-round timing retained");
    await io.send("\x1b[C"); expect(io.text()).toContain("task-b");
    expect(io.text()).not.toContain("task-a trace read failed");
    expect(io.text()).toContain("No per-round timing retained");
  } finally {io.close(); Object.assign(colors.feedback, {errorMs: previous});}
});

test("late trace failure cannot appear after selecting another task and returning", async () => {
  let reject!: (error: Error) => void, visits = 0;
  const io = mount((name, args) => {
    if (name === "runtime.usage") return {tasks: [task("task-a"), task("task-b")], page: {}};
    if (args.trace_id === "task-a" && visits++ === 0) return new Promise((_, no) => {reject = no;});
    return {events: [], has_more: false};
  });
  try {
    await wait(); await io.send("\x1b[C"); await io.send("\x1b[D");
    reject(Error("Old task trace failed")); await wait();
    expect(io.text()).toContain("task-a"); expect(io.text()).not.toContain("Old task trace failed");
    expect(io.text()).not.toContain("Round records not loaded");
  } finally {io.close();}
});

test("task pagination finishes across task navigation without mixing traces or leaving refresh disabled", async () => {
  let resolve!: (data: any) => void, reads = 0;
  const io = mount((name, args) => {
    if (name === "runtime.trace") return {events: [], has_more: false};
    if (args.cursor) return new Promise(yes => {resolve = yes;});
    reads++;
    return {tasks: [task("task-a"), task("task-b")], page: {has_more: true, next_cursor: "older"}};
  });
  try {
    await wait(); await io.send("\t"); await io.send("\r");
    await io.send("\t"); await io.send("\x1b[C");
    resolve({tasks: [task("task-c")], page: {has_more: false}}); await wait();
    expect(io.text()).toContain("3 loaded tasks"); expect(io.text()).toContain("task-b");
    await io.send("\t"); await io.send("\r");
    expect(reads).toBe(2);
    expect(io.calls.filter(call => call.name === "runtime.trace").map(call => call.args.trace_id)).toEqual(["task-a", "task-b", "task-a"]);
  } finally {io.close();}
});
