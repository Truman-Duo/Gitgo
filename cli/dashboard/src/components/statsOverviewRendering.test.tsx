import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import chalk from "chalk";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {StatsOverview} from "./StatsOverview.js";
import {colors} from "../theme/index.js";

const wait = (ms = 90) => new Promise(resolve => setTimeout(resolve, ms));
const usage = () => ({complete: false, summary: {input_tokens: 10000, output_tokens: 2000, provider_calls: 25, task_count: 50},
  tasks: Array.from({length: 50}, (_, index) => ({project: `project-${index % 6}`, task_id: `task-${index}`, created_at: "2026-11-08T00:00:00Z", duration_ms: 1000, input_tokens: 200, output_tokens: 40})),
  projects: Array.from({length: 14}, (_, index) => ({project: `project-${index}`, input_tokens: 500, output_tokens: 100,
    provider_calls: 5, task_count: 5, ...(index > 7 ? {error: "Workspace does not exist"} : {})})), daily: []});
function mount({width = 124, height = 30, language = "zh", altScreen = false, query = usage}:
  {width?: number; height?: number; language?: "en" | "zh"; altScreen?: boolean; query?: () => any} = {}) {
  const previous = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: width, rows: height}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const calls: string[] = [];
  const client: any = {async callTool(operation: string) {
    calls.push(operation);
    return query();
  }};
  const view = () => <InputProvider><LanguageContext.Provider value={language}><Box width={stdout.columns} height={stdout.rows} flexDirection="column">
    <StatsOverview client={client} cols={stdout.columns} onDismiss={() => {}}/>
  </Box></LanguageContext.Provider></InputProvider>;
  const root = renderSync(view(), {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const ink: any = instances.get(stdout); ink.setAltScreenActive(altScreen); ink.onRender();
  return {calls,
    lines() {const screen = ink.frontFrame.screen; return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? "").join("").trimEnd());},
    async send(value: string) {stdin.write(value); await wait();},
    async resize(w: number, h: number) {stdout.columns = w; stdout.rows = h; stdout.emit("resize"); root.rerender(view()); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previous;},
  };
}

for (const altScreen of [false, true]) test(`stats header remains one row through recent-tasks to projects (alt-screen=${altScreen})`, async () => {
  const io = mount({altScreen});
  try {
    await wait();
    const assertHeader = () => {
      const lines = io.lines(), text = lines.join("\n");
      for (const title of ["每日用量", "最近任务", "项目汇总"]) expect(text.split(title).length - 1, JSON.stringify({title, header: lines.slice(0, 7)})).toBe(1);
      const headerRows = lines.flatMap((line, index) => ["每日用量", "最近任务", "项目汇总"].some(title => line.includes(title)) ? [index] : []);
      expect(headerRows).toHaveLength(1);
      expect(text.split("用量总览").length - 1).toBe(1);
      expect(lines[1]).toContain("用量总览");
      expect(headerRows[0]).toBe(2);
    };
    assertHeader();
    for (const key of ["\x1b[C", "\x1b[C", "\x1b[C", "\x1b[D", "\x1b[D", "\x1b[C", "\x1b[C"]) {await io.send(key); assertHeader();}
    expect(io.lines().join("\n")).toContain("Workspace does not exist");
    expect(io.calls).toEqual(["runtime.usage"]);
  } finally {io.close();}
});

test("partial usage warning expires, while the subtotal and unavailable project facts remain readable", async () => {
  const previous = colors.feedback.warningMs; Object.assign(colors.feedback, {warningMs: 180});
  const io = mount();
  try {
    await wait();
    expect(io.lines().join("\n")).toContain("部分项目不可用，汇总尚不完整。");
    await wait(200);
    expect(io.lines().join("\n")).not.toContain("部分项目不可用，汇总尚不完整。");
    expect(io.lines().join("\n")).toContain("已读取合计");
    await io.send("\x1b[C"); await io.send("\x1b[C");
    expect(io.lines().join("\n")).toContain("Workspace does not exist");
    expect(io.calls).toEqual(["runtime.usage"]);
  } finally {io.close(); Object.assign(colors.feedback, {warningMs: previous});}
});

test("usage warning leaves with its tab and is not replayed on returning", async () => {
  const io = mount();
  try {
    await wait(); expect(io.lines().join("\n")).toContain("部分项目不可用");
    await io.send("\x1b[C"); expect(io.lines().join("\n")).not.toContain("部分项目不可用");
    await io.send("\x1b[D"); expect(io.lines().join("\n")).not.toContain("部分项目不可用");
    expect(io.lines().join("\n")).toContain("已读取合计");
    expect(io.calls).toEqual(["runtime.usage"]);
  } finally {io.close();}
});

for (const failed of [false, true]) test(`late usage result updates data without a stale alert after leaving and returning (failed=${failed})`, async () => {
  let resolve!: (data: any) => void, reject!: (error: Error) => void;
  const io = mount({query: () => new Promise((yes, no) => {resolve = yes; reject = no;})});
  try {
    await wait(); await io.send("\x1b[C"); await io.send("\x1b[D");
    if (failed) reject(new Error("Old query failed")); else resolve(usage());
    await wait();
    const text = io.lines().join("\n");
    expect(text).not.toContain("部分项目不可用"); expect(text).not.toContain("Old query failed");
    expect(text).toContain(failed ? "用量尚未读取" : "已读取合计");
    expect(io.calls).toEqual(["runtime.usage"]);
  } finally {io.close();}
});

test("usage failure expires and a successful explicit refresh replaces the missing-data state", async () => {
  const previous = colors.feedback.errorMs; Object.assign(colors.feedback, {errorMs: 180});
  let count = 0;
  const io = mount({query: () => {if (count++ === 0) throw new Error("Usage query failed"); return {...usage(), complete: true};}});
  try {
    await wait(); expect(io.lines().join("\n")).toContain("Usage query failed");
    expect(io.lines().join("\n")).not.toContain("0 词元");
    await wait(200); expect(io.lines().join("\n")).not.toContain("Usage query failed");
    await io.send("\r"); await io.send("\t"); await io.send("\x1b[C"); await io.send("\r");
    const text = io.lines().join("\n");
    expect(text).toContain("12,000 词元"); expect(text).not.toContain("用量尚未读取"); expect(text).not.toContain("已读取合计");
    expect(io.calls).toEqual(["runtime.usage", "runtime.usage"]);
  } finally {io.close(); Object.assign(colors.feedback, {errorMs: previous});}
});

test("a failed refresh retains the last recorded subtotal after its error expires", async () => {
  const previous = colors.feedback.errorMs; Object.assign(colors.feedback, {errorMs: 180});
  let count = 0;
  const io = mount({query: () => {if (count++ === 0) return usage(); throw Error("Refresh read failed");}});
  try {
    await wait(); await io.send("\r"); await io.send("\t"); await io.send("\x1b[C"); await io.send("\r");
    expect(io.lines().join("\n")).toContain("Refresh read failed");
    await wait(200);
    const text = io.lines().join("\n");
    expect(text).not.toContain("Refresh read failed"); expect(text).toContain("上次读取 · 已读取合计");
    expect(text).toContain("12,000 词元"); expect(io.calls).toEqual(["runtime.usage", "runtime.usage"]);
  } finally {io.close(); Object.assign(colors.feedback, {errorMs: previous});}
});

test("narrow and short usage layout survives list changes and terminal resizing", async () => {
  const io = mount({width: 44, height: 14, language: "en"});
  try {
    await wait();
    for (const [width, height] of [[44, 14], [124, 30], [60, 14], [124, 30]]) {
      await io.resize(width!, height!);
      await io.send("\x1b[C"); await io.send("\x1b[C"); await io.send("\x1b[D");
      const text = io.lines().join("\n");
      for (const title of ["Usage overview", "Recent tasks"]) expect(text.split(title).length - 1, JSON.stringify({width, height, title, lines: io.lines()})).toBe(1);
      expect(text.split("Projects").length - 1).toBeLessThanOrEqual(1);
      expect(io.lines()[1]).toContain("Usage overview");
      expect(io.lines()[2]).toContain("Recent tasks");
      expect(text).toContain("Refresh");
      await io.send("\x1b[D");
    }
    expect(io.calls).toEqual(["runtime.usage"]);
  } finally {io.close();}
});
