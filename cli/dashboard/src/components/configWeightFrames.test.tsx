import React from "react";
import {test} from "node:test";
import assert from "node:assert/strict";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {Parser, type Action} from "../../vendor/ink/src/core/termio.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {ConfigPanel} from "./ConfigPanel.js";
import {Overview} from "./Overview.js";
import {App} from "./App.js";

// Replay emitted bytes independently of Ink's cell/style caches. These tests
// check terminal-output semantics, not visible-terminal visual acceptance.
class OutputCells {
  parser = new Parser();
  cells = new Map<string, {char: string; bold: boolean}>();
  x = 0; y = 0; saved = {x: 0, y: 0};
  constructor(public width: number, public height: number) {}
  feed(bytes: string) {for (const action of this.parser.feed(bytes)) this.apply(action);}
  private erase(matches: (x: number, y: number) => boolean) {
    for (const key of this.cells.keys()) {
      const [x, y] = key.split(",").map(Number);
      if (matches(x!, y!)) this.cells.delete(key);
    }
  }
  private apply(action: Action) {
    if (action.type === "text") {
      for (const g of action.graphemes) {
        if (g.value === "\r") {this.x = 0; continue;}
        if (g.value === "\n") {this.y++; continue;}
        if (g.value === "\r\n") {this.x = 0; this.y++; continue;}
        if (this.x >= this.width) {this.x = 0; this.y++;}
        this.cells.set(`${this.x},${this.y}`, {char: g.value, bold: action.style.bold});
        this.x += g.width;
      }
    } else if (action.type === "cursor") {
      const c = action.action;
      switch (c.type) {
        case "position": this.x = c.col - 1; this.y = c.row - 1; break;
        case "column": this.x = c.col - 1; break;
        case "row": this.y = c.row - 1; break;
        case "save": this.saved = {x: this.x, y: this.y}; break;
        case "restore": ({x: this.x, y: this.y} = this.saved); break;
        case "move":
          if (c.direction === "up") this.y -= c.count;
          if (c.direction === "down") this.y += c.count;
          if (c.direction === "back") this.x -= c.count;
          if (c.direction === "forward") this.x += c.count;
          this.x = Math.max(0, Math.min(this.width - 1, this.x));
          this.y = Math.max(0, Math.min(this.height - 1, this.y));
          break;
        case "nextLine": this.x = 0; this.y += c.count; break;
        case "prevLine": this.x = 0; this.y -= c.count; break;
      }
    } else if (action.type === "erase") {
      const e = action.action;
      if (e.type === "display") this.erase((x, y) => e.region === "all" || e.region === "scrollback"
        || (e.region === "toEnd" ? y > this.y || (y === this.y && x >= this.x) : y < this.y || (y === this.y && x <= this.x)));
      if (e.type === "line") this.erase((x, y) => y === this.y && (e.region === "all" || (e.region === "toEnd" ? x >= this.x : x <= this.x)));
      if (e.type === "chars") this.erase((x, y) => y === this.y && x >= this.x && x < this.x + e.count);
    }
  }
}

test("complete config emits correct weights on every frame through load, selection, edit and tabs", async () => {
  const previous = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 100, rows: 25});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const output = new OutputCells(stdout.columns, stdout.rows);
  output.feed("\x1b[1m\x1b[2m"); // A previous writer left both intensity bits active.
  let checkFrames = false, frameCount = 0;
  let ink: any;
  const mismatches: unknown[] = [];
  stdout.on("data", (bytes: Buffer) => {
    output.feed(bytes.toString());
    if (!checkFrames) return;
    frameCount++;
    const screen = ink.frontFrame.screen;
    for (let y = 0; y < screen.height; y++) for (let x = 0; x < screen.width; x++) {
      const cell = cellAt(screen, x, y);
      if (!cell?.char?.trim()) continue;
      const actual = output.cells.get(`${x},${y}`);
      const bold = ink.stylePool.get(cell.styleId).some((c: any) => c.code === "\x1b[1m");
      if (actual?.char !== cell.char || actual.bold !== bold) mismatches.push({frame: frameCount, x, y, expected: {char: cell.char, bold}, actual});
    }
  });
  let releaseConfig: (() => void) | undefined;
  const loaded = new Promise<void>(resolve => {releaseConfig = resolve;});
  const client: any = {async callTool(name: string) {
    if (name === "product.version") return {version: "1.0.0-alpha.3"};
    if (name === "config.terminals") return {options: [{id: "auto", label: "Current terminal", available: true}], warnings: []};
    if (name === "config.get") {await loaded; return {language: "zh", verbose: false, auto_compact: true,
      prompt_profiles: [{id: "default", label: "Baseline"}]};}
    return {};
  }};
  function Fixture() {
    const input = useTextInput();
    return <InputProvider><LanguageContext.Provider value="zh"><Box width="100%" height={stdout.rows} flexDirection="column">
      <ConfigPanel client={client} project="test" cmdInput={input} onFooter={() => {}} onBack={() => {}}/>
    </Box></LanguageContext.Provider></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  ink = instances.get(stdout);
  const wait = (ms = 85) => new Promise(resolve => setTimeout(resolve, ms));
  const send = async (keys: string) => {stdin.write(keys); await wait();};
  const boldWords = () => {
    const lines: string[] = [];
    for (let y = 0; y < stdout.rows; y++) {
      let line = "";
      for (let x = 0; x < stdout.columns; x++) {const c = output.cells.get(`${x},${y}`); if (c?.bold) line += c.char;}
      if (line.trim()) lines.push(line.trim());
    }
    return lines;
  };
  try {
    stdout.write("\x1b[2J\x1b[H");
    checkFrames = true; ink.setAltScreenActive(true); ink.onRender();
    await wait(); assert.deepEqual(boldWords(), ["通用设置"]);
    releaseConfig!(); await wait(); assert.deepEqual(boldWords(), ["通用设置"]);
    await send("\r"); assert.deepEqual(boldWords(), ["通用设置", "详细显示OFF"]);
    for (let i = 0; i < 6; i++) await send("\x1b[B");
    assert.deepEqual(boldWords(), ["通用设置", "备用搜索引擎● Duckduckgo"]);
    await send("\x1b[A"); assert.deepEqual(boldWords(), ["通用设置", "网页搜索服务商 → SearXNG"]);
    await send("\r"); await wait(500); // Label-only edit blink also changes dirty cells.
    await send("\x1b"); await send("\x1b"); assert.deepEqual(boldWords(), ["通用设置"]);
    await send("\x1b[C"); assert.ok(boldWords().includes("模型服务商"));
    await send("\x1b[D"); assert.deepEqual(boldWords(), ["通用设置"]);
    stdout.columns = 55; output.width = 55; stdout.emit("resize"); await wait();
    assert.deepEqual(boldWords(), ["通用设置"]);
    assert.ok(frameCount > 12);
    assert.deepEqual(mismatches.slice(0, 8), []);
  } finally {
    checkFrames = false; root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previous;
  }
});

test("all project-list columns retain their header weight across selection and refresh", async () => {
  const previous = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 100, rows: 25});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const output = new OutputCells(100, 25);
  stdout.on("data", (bytes: Buffer) => output.feed(bytes.toString()));
  const project: any = {name: "示例项目", workspace: "C:/example", stateAvailable: true, daemonOnline: false,
    activeProcessCount: 0, waitingProcessCount: 0, finishedProcessCount: 1};
  const fixture = (sel: number, listActive = true) => <Box width="100%" height={25} flexDirection="column">
    <Overview projects={[project, {...project, name: "第二项目"}]} sel={sel} mode="NORMAL" cols={100} listActive={listActive} language="zh"/>
  </Box>;
  const root = renderSync(fixture(0), {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const ink: any = instances.get(stdout);
  try {
    stdout.write("\x1b[2J\x1b[H"); ink.setAltScreenActive(true); ink.onRender();
    for (const active of [true, false, true]) for (const selected of [0, 1, 0]) {
      root.rerender(fixture(selected, active)); await new Promise(resolve => setTimeout(resolve, 85));
      const screen = ink.frontFrame.screen;
      for (let y = 0; y < screen.height; y++) for (let x = 0; x < screen.width; x++) {
        const cell = cellAt(screen, x, y);
        if (cell?.char !== "项" && cell?.char !== "进") continue;
        if (y !== 1) continue;
        const physical = output.cells.get(`${x},${y}`);
        assert.equal(physical?.char, cell.char);
        assert.equal(physical?.bold, true, `header at ${x},${y} should be bold`);
      }
    }
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previous;}
});

test("complete App retains project headings and selected tab weight while unrelated data arrives", async () => {
  const previous = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 100, rows: 28});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const output = new OutputCells(100, 28);
  stdout.on("data", (bytes: Buffer) => output.feed(bytes.toString()));
  const subscribers = new Set<(event: any) => void>();
  const client: any = {ready: true, close() {}, subscribeEvents(fn: (event: any) => void) {subscribers.add(fn); return () => subscribers.delete(fn);},
    async callTool(name: string) {
      if (name === "project.overview") return {projects: [{name: "示例", workspace: "C:/example", runtime_summary: {state_available: true, daemon_online: false}}]};
      if (name === "config.get") return {language: "zh", verbose: false, auto_compact: true};
      if (name === "product.version") return {version: "1.0.0-alpha.3"};
      if (name === "config.terminals") return {options: [{id: "auto", label: "Current terminal", available: true}], warnings: []};
      return {};
    }};
  const root = renderSync(<InputProvider><App client={client}/></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const ink: any = instances.get(stdout);
  const wait = (ms = 130) => new Promise(resolve => setTimeout(resolve, ms));
  const send = async (keys: string) => {stdin.write(keys); await wait();};
  function textWeight(text: string) {
    const screen = ink.frontFrame.screen;
    for (let y = 0; y < screen.height; y++) for (let x = 0; x < screen.width; x++) {
      let content = "";
      for (let col = x; col < screen.width; col++) content += cellAt(screen, col, y)?.char || "";
      if (!content.startsWith(text)) continue;
      return {logical: ink.stylePool.get(cellAt(screen, x, y)!.styleId).some((c: any) => c.code === "\x1b[1m"), physical: output.cells.get(`${x},${y}`)?.bold};
    }
    throw Error("Missing text " + text);
  }
  try {
    stdout.write("\x1b[2J\x1b[H"); ink.setAltScreenActive(true); ink.onRender(); await wait();
    for (let update = 0; update < 4; update++) {
      for (const heading of ["项目名称", "进程", "项目位置"]) assert.deepEqual(textWeight(heading), {logical: true, physical: true}, heading);
      for (const fn of subscribers) fn({event: "project_summary_updated", project: "示例", summary: {state_available: true, finished_process_count: update + 1}});
      await wait();
    }
    await send("/"); await send("config"); await send("\r");
    assert.deepEqual(textWeight("通用设置"), {logical: true, physical: true});
    await send("\x1b[C"); assert.deepEqual(textWeight("模型服务商"), {logical: true, physical: true});
    await send("\r"); await wait(650);
    assert.deepEqual(textWeight("模型服务商"), {logical: true, physical: true});
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previous;}
});
