import React, {useState} from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Box, Text} from "@anthropic/ink";
import stripAnsi from "strip-ansi";
import {InputLayer, InputProvider} from "../input/runtime.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {RuntimeMenu} from "./RuntimeMenu.js";
import {RuntimeContextPanel} from "./RuntimeContextPanel.js";
import {governanceSections} from "./governancePresentation.js";
import {enterProjectEffect} from "../effects/run.js";
import type {BackendClient} from "../backend/client.js";
const wait = () => new Promise(resolve => setTimeout(resolve, 80));
function streams() {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin; stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 120; stdout.rows = 36;
  const stderr: any = new PassThrough(); let output = "";
  stdout.on("data", (chunk: Buffer) => {output += chunk.toString();}); stderr.on("data", () => {});
  return {stdin, stdout, stderr, output: () => stripAnsi(output)};
}
const client: BackendClient = {ready: true, close() {}, async callTool(operation) {
  if (operation === "governance.quality") return {suggestion_count: 0, by_type: {}};
  return {};
}};
for (let tab = 0; tab < 6; tab++) {
  test(`runtime tab ${tab} returns to header then workspace with two Esc presses`, async () => {
    const io = streams();
    function Fixture() {
      const [closed, setClosed] = useState(false);
      const input = useTextInput();
      return closed ? <Text>WORKSPACE RETURNED</Text> : <RuntimeMenu client={client} project="demo" processes={{}}
        cols={120} rows={36} cmdInput={input} onFooter={() => {}} onSelect={() => {}}
        onDismiss={() => setClosed(true)}/>;
    }
    const root = renderSync(<InputProvider><Fixture/></InputProvider>, {...io, patchConsole: false, exitOnCtrlC: false});
    try {
      await wait();
      for (let i = 0; i < tab; i++) { io.stdin.write("\x1b[C"); await wait(); }
      if (tab === 2) {
        const lines = io.output().split("\n");
        const lastQuality = lines.findLastIndex(line => line.includes("Quality") && line.includes("Patterns"));
        expect(lines[lastQuality - 1]).toContain("─");
      }
      io.stdin.write("\r"); await wait();
      io.stdin.write("\x1b"); await wait();
      expect(io.output()).not.toContain("WORKSPACE RETURNED");
      io.stdin.write("\x1b"); await wait();
      expect(io.output()).toContain("WORKSPACE RETURNED");
    } finally {root.unmount(); root.cleanup(); io.stdin.destroy(); io.stdout.destroy(); io.stderr.destroy();}
  });
}
test("missing workspace never navigates, success waits for backend preflight and stale replies are ignored", async () => {
  const actions: any[] = [];
  let resolve!: (value: any) => void;
  const waiting = {...client, callTool: () => new Promise<any>(r => {resolve = r;})};
  const entry = enterProjectEffect(waiting, "demo", a => actions.push(a));
  expect(actions).toEqual([]);
  resolve({ok: true}); await entry;
  expect(actions[0]).toMatchObject({type: "navigate", scene: "workspace", patch: {activeProject: "demo"}});
  actions.length = 0;
  await enterProjectEffect({...client, callTool: async () => {throw new Error("WORKSPACE_NOT_FOUND: C:/gone");}}, "gone", a => actions.push(a));
  expect(actions).toHaveLength(1);
  expect(actions[0]).toMatchObject({type: "set_cmd_result"});
  expect(actions[0].text).toContain("C:/gone");
  actions.length = 0;
  await enterProjectEffect(client, "demo", a => actions.push(a), () => false);
  expect(actions).toEqual([]);
});
test("governance summaries retain zero, distinguish missing metrics and explain empty samples", () => {
  const quality = governanceSections(0, {suggestion_count: 0, by_type: {scan: {total: 2, acceptance_rate: 0}}});
  expect(quality[0]!.lines[0]).toContain("0 suggestions");
  expect(quality[1]!.lines[0]).toContain("0% accepted");
  expect(governanceSections(0, {})[0]!.lines[0]).toContain("— suggestions");
  expect(governanceSections(1, {trial_impact: {total_accepted: 0}})[2]!.lines).toContain("No accepted trial samples yet.");
  expect(governanceSections(2, {items: []})).toEqual([]);
  expect(governanceSections(3, {releases: []})).toEqual([]);
  const feed = governanceSections(2, {items: [{operation: "completion_gate", timestamp: "2026-10-17T00:00:00Z", status: "info", detail: {process_id: "abcdefgh-long-internal-id", reason: "provider_phase"}}]});
  expect(feed[0]!.title).toContain("completion gate");
  expect(feed[0]!.lines).toContain("Process abcdefgh  ·  Unknown event");
});
test("context process selection retains identity after polling order changes", async () => {
  const io = streams(); let change!: () => void;
  const process = (id: string, name: string, model: string, created: string) => ({process_id: id, display_name: name,
    model_id: model, created_at: created, context: {limit: 10000, used_tokens: 500, epoch: 0}, actor_kind: "worker"} as any);
  const a = process("a", "a very long process name with spaces 中文名称", "model-a", "1");
  const b = process("b", "another long label repeated many times", "model-b", "2");
  function Fixture() {
    const [rows, setRows] = useState({a, b});
    change = () => setRows({a: {...a, created_at: "3"}, b});
    return <RuntimeContextPanel project="demo" processes={rows} onDismiss={() => {}}/>;
  }
  const root = renderSync(<InputProvider><Fixture/></InputProvider>, {...io, patchConsole: false, exitOnCtrlC: false});
  try {
    await wait(); io.stdin.write("\x1b[C"); await wait();
    expect(io.output()).toContain("model-b");
    change(); await wait();
    expect(io.output().slice(-2000)).toContain("model-b");
  } finally {root.unmount(); root.cleanup(); io.stdin.destroy(); io.stdout.destroy(); io.stderr.destroy();}
});


test("runtime Feed owns its paging keys and preview tools cannot archive before entering", async () => {
  const io = streams(); const calls: any[] = [];
  const backend: BackendClient = {...client, async callTool(operation, args = {}) {
    calls.push({operation,args});
    if(operation === "governance.feed") return {items: [{operation: "scan", timestamp: "2026-10-17T00:00:00Z", status: "success"}],page: {has_more: !args.cursor,next_cursor: args.cursor ? null : "older-page"}};
    if(operation === "runtime.tools.list") return {tools: [{name: "test-tool",version: 1,state: "active"}]};
    return {};
  }};
  function Fixture() {
    const input = useTextInput();
    return <InputLayer priority={100}><RuntimeMenu client={backend} project="demo" processes={{}}
      cols={120} rows={36} cmdInput={input} onFooter={() => {}} onSelect={() => {}} onDismiss={() => {}}/></InputLayer>;
  }
  const root = renderSync(<InputProvider><Fixture/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false});
  try {
    await wait();
    for(let i=0;i<2;i++){io.stdin.write("\x1b[C");await wait();}
    io.stdin.write("\r");await wait();
    for(let i=0;i<2;i++){io.stdin.write("\x1b[C");await wait();}
    io.stdin.write("\x1b[6~");await wait();
    expect(calls).toContainEqual({operation:"governance.feed",args:{project:"demo",limit:20,cursor:"older-page"}});
    io.stdin.write("\x1b");await wait();
    for(let i=0;i<3;i++){io.stdin.write("\x1b[C");await wait();}
    expect(calls.some(c=>c.operation==="runtime.tools.archive")).toBe(false);
    io.stdin.write("\r");await wait();
    expect(calls.some(c=>c.operation==="runtime.tools.archive")).toBe(false);
    io.stdin.write("\r");await wait();
    expect(calls).toContainEqual({operation:"runtime.tools.archive",args:{project:"demo",name:"test-tool"}});
  } finally {root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});


for (const height of [24, 36, 48]) {
  test(`lesson shortcuts stay at the viewport bottom at ${height} rows`, async () => {
    const io = streams(); io.stdout.rows = height;
    function Fixture() {
      const input = useTextInput();
      return <Box width={120} height={height} flexDirection="column"><RuntimeMenu client={client} project="demo" processes={{}}
        cols={120} rows={height} cmdInput={input} onFooter={() => {}} onSelect={() => {}} onDismiss={() => {}}/></Box>;
    }
    const root=renderSync(<InputProvider><Fixture/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false});
    try {
      await wait();
      const lines=io.output().split("\n");
      const header=lines.findLastIndex(line=>line.includes("lesson")&&line.includes("contract")&&line.includes("governance"));
      const footer=lines.findLastIndex(line=>line.includes("P proposals · S search"));
      expect(footer-header).toBe(height-2);
    } finally {root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
  });
}
