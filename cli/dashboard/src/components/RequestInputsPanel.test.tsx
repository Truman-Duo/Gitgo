import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import chalk from "chalk";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";
import {RequestInputsPanel} from "./RequestInputsPanel.js";
import {StatsPanel} from "./StatsPanel.js";
import type {TraceRecord} from "../traceView.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 90));
const event = (seq: number, ref = `saved-${seq}`): TraceRecord => ({seq, time: "2026-11-07T00:00:00Z", process_id: "worker", event: "provider_request_started",
  detail_ref: ref, request_parameters: {model: "fixed-model", protocol: "openai_responses", effort: "medium", context_window: 512000,
    max_output_tokens: 128000, configuration: {sources: {context_window: "reference"}, effort_source: "user"}, state: "dispatching", server_confirmed: false}});
function snapshot(content = "Rules from the old request") {
  return {representation: "canonical_provider_input", messages: [{role: "system", content}], tools: [],
    input_provenance: {schema_version: 1, state: "dispatching", server_confirmed: false, wire_body_sha256: "old-body-hash", sources: [
      {message_index: 0, message_type: "runtime_policy", content_sha256: "old-content-hash", sections: [
        {name: "Project rules", provenance: "project:rules", version: "old-version"}, {name: "Lesson", provenance: "project:lesson", version: null}]}]}};
}
function mount({events = [event(1)], stats = false, language = "en", read = async () => snapshot()}:
  {events?: TraceRecord[]; stats?: boolean; language?: "en" | "zh"; read?: (ref: string) => Promise<any>} = {}) {
  const previousColor = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode: () => stdin, ref: () => stdin, unref: () => stdin});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 110, rows: 32}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const calls: {name: string; args: any}[] = []; let dismissed = 0;
  const client: any = {async callTool(name: string, args: any) {
    calls.push({name, args});
    if (name === "runtime.usage") return {tasks: [{task_id: "turn", status: "completed", provider_calls: events.length, tool_calls: 0}], page: {}};
    if (name === "runtime.trace" && args.action === "read") return {events, has_more: false};
    if (name === "runtime.trace" && args.action === "detail") return {detail: await read(args.ref)};
    throw Error("No current config/file reads are permitted: " + name);
  }};
  const view = () => <LanguageContext.Provider value={language}><InputProvider><Box width={stdout.columns} height={stdout.rows} flexDirection="column">
    <ModuleHelpBoundary onFooter={() => {}}>{() => stats ? <StatsPanel client={client} project="test" cols={stdout.columns} onDismiss={() => dismissed++}/>
      : <RequestInputsPanel client={client} project="test" events={events} onDismiss={() => dismissed++}/>}</ModuleHelpBoundary>
  </Box></InputProvider></LanguageContext.Provider>;
  const root = renderSync(view(), {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {calls, dismissed: () => dismissed,
    async send(value: string) {stdin.write(value); await wait();},
    text() {const screen = (instances.get(stdout) as any).frontFrame.screen; return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || "").join("")).join("\n");},
    async resize(w: number, h: number) {stdout.columns = w; stdout.rows = h; stdout.emit("resize"); root.rerender(view()); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previousColor;},
  };
}

for (const language of ["en", "zh"] as const) test(`saved request values and source versions are read lazily without substituting today's files (${language})`, async () => {
  const io = mount({language});
  try {
    await wait(); expect(io.calls).toHaveLength(0); await io.send("\r");
    expect(io.calls).toEqual([{name: "runtime.trace", args: {project: "test", action: "detail", ref: "saved-1"}}]);
    const text = io.text(); expect(text).toContain("fixed-model"); expect(text).toContain("medium"); expect(text).toContain("512000");
    expect(text).toContain(language === "zh" ? "服务端是否接受仍未确认" : "server acceptance is unconfirmed");
    expect(text).not.toContain("Rules from the old request");
    await io.send("\r"); expect(io.text()).toContain("old-version"); expect(io.text()).toContain("Rules from the old request");
    expect(io.text()).toContain(language === "zh" ? "版本: 未记录" : "Version: Not recorded");
    await io.send("\x1b"); await io.send("\x1b"); expect(io.text()).toContain(language === "zh" ? "已记录的模型输入" : "Recorded model inputs");
    expect(io.calls).toHaveLength(1);
  } finally {io.close();}
});

test("legacy preparation records keep missing actual parameters and provenance unknown", async () => {
  const legacy = event(1); delete legacy.request_parameters;
  const io = mount({events: [legacy], read: async () => ({representation: "prepared_runtime_projection", messages: [{content: "old preparation"}], tools: []})});
  try {
    await wait(); await io.send("\r"); expect(io.text()).toContain("Model: Not recorded");
    expect(io.text()).toContain("An exact source snapshot was not recorded"); expect(io.text()).not.toContain("512000");
    expect(io.text()).not.toContain("old preparation");
  } finally {io.close();}
});

test("leaving a pending detail prevents its late snapshot from replacing another request", async () => {
  let complete: (value: any) => void = () => {};
  const io = mount({events: [event(1), event(2)], read: ref => ref === "saved-1" ? new Promise(resolve => {complete = resolve;}) : Promise.resolve(snapshot("Second request content"))});
  try {
    await wait(); await io.send("\r"); await io.send("\x1b"); await io.send("\x1b[B"); await io.send("\r");
    complete(snapshot("Stale first request")); await wait(); await io.send("\r");
    expect(io.text()).toContain("Second request content"); expect(io.text()).not.toContain("Stale first request");
    expect(io.calls.map(call => call.args.ref)).toEqual(["saved-1", "saved-2"]);
  } finally {io.close();}
});

test("large saved source text uses bounded chunks and remains readable after resize", async () => {
  const io = mount({read: async () => snapshot("FIRST\n" + "x".repeat(12000) + "\nSECOND")});
  try {
    await wait(); await io.send("\r"); await io.send("\r"); expect(io.text()).toContain("FIRST"); expect(io.text()).not.toContain("SECOND");
    await io.send("\x1b[C"); expect(io.text()).toContain("SECOND"); expect(io.text()).not.toContain("FIRST");
    await io.resize(38, 14); await io.send("\x1b"); await io.resize(110, 32);
    expect(io.calls).toHaveLength(1); expect(io.text()).toContain("Project rules");
  } finally {io.close();}
});

test("waterfall opens recorded request inputs with the shared action group and preserves its turn", async () => {
  const io = mount({stats: true});
  try {
    await wait(); expect(io.text()).not.toContain("1 / 1"); expect(io.text()).toContain("1 loaded tasks");
    await io.send("\t"); await io.send("\x1b[C"); await io.send("\r");
    expect(io.text()).toContain("Recorded model inputs"); expect(io.calls).toHaveLength(2);
    await io.send("\r"); await io.send("\r"); expect(io.text()).toContain("old-version");
    await io.send("\x1b"); await io.send("\x1b"); await io.send("\x1b");
    expect(io.text()).toContain("Turn waterfall"); expect(io.calls.filter(call => call.name === "runtime.usage")).toHaveLength(1);
  } finally {io.close();}
});
