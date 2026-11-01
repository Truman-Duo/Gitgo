import React from "react";
import {expect, test} from "bun:test";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {Parser, type Action} from "../../vendor/ink/src/core/termio.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {displayWidth} from "../theme/index.js";
import {GeneralTab} from "./config/GeneralTab.js";

const wait = (ms = 85) => new Promise(resolve => setTimeout(resolve, ms));
function mount(language: "en" | "zh") {
  const previous = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 100, rows: 25});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const parser = new Parser(), emitted: Action[] = [];
  stdout.on("data", (data: Buffer) => emitted.push(...parser.feed(data.toString())));
  const calls: {name: string; args: any}[] = [];
  let footer: any;
  const client: any = {async callTool(name: string, args: any) {
    calls.push({name, args});
    if (name === "product.version") return {version: "1.0.0-alpha.3"};
    if (name === "config.terminals") return {options: [{id: "auto", label: "Current terminal", available: true}], warnings: []};
    if (name === "config.get") return {language, verbose: false, auto_compact: true, agent_routing: "owner",
      prompt_profiles: [{id: "default", label: "Baseline"}]};
    return {ok: true};
  }};
  function Fixture() {const input = useTextInput(); return <InputProvider><LanguageContext.Provider value={language}>
    <Box width="100%" height={stdout.rows} flexDirection="column"><GeneralTab client={client} project="test" cmdInput={input} contentFocused
      onFooter={value => {footer = value;}} report={() => {}} shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}/></Box>
  </LanguageContext.Provider></InputProvider>;}
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const ink: any = instances.get(stdout);
  ink.setAltScreenActive(true); ink.onRender();
  const line = (y: number) => {const screen = ink.frontFrame.screen; return Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? " ").join("");};
  const locate = (text: string) => {
    const screen = ink.frontFrame.screen;
    for (let y = 0; y < screen.height; y++) {const row = line(y), at = row.indexOf(text); if (at >= 0) return {x: displayWidth(row.slice(0, at)), y};}
    throw Error("Missing rendered text: " + text);
  };
  return {calls, line, locate, emitted, footer: () => footer,
    bold(at: {x: number; y: number}) {return ink.stylePool.get(cellAt(ink.frontFrame.screen, at.x, at.y)!.styleId).some((code: any) => code.code === "\x1b[1m");},
    async send(value: string) {stdin.write(value); await wait();},
    async resize(width: number) {stdout.columns = width; stdout.emit("resize"); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previous;},
  };
}

for (const language of ["en", "zh"] as const) test(`General aligns terminal-cell columns and only the selected field is bold (${language})`, async () => {
  const io = mount(language);
  try {
    await wait();
    const values = language === "zh" ? ["OFF", "中文", "ON", "原负责进程", "系统默认", "服务商 → SearXNG", "●", "未配置", "自动选择", "Baseline", "并排"]
      : ["OFF", "English", "ON", "Original owner", "System default", "Provider → SearXNG", "●", "Not configured", "Automatic", "Baseline", "Split"];
    for (const width of [100, 40]) {
      await io.resize(width);
      const positions = values.map(value => io.locate(value));
      expect(new Set(positions.map(at => at.x)).size).toBe(1);
      expect(io.locate("Duckduckgo").x).toBe(io.locate("●").x + 2);
      expect(io.bold(positions[0]!)).toBe(true);
      for (const at of positions.slice(1)) expect(io.bold(at)).toBe(false);
      expect(io.bold(io.locate("Gitgo Version"))).toBe(false);
    }
    const characters = io.emitted.flatMap(action => action.type === "text" ? action.graphemes
      .filter(g => g.value.trim()).map(g => ({text: g.value, bold: action.style.bold})) : []);
    const owner = language === "zh" ? "原负责进程" : "Originalowner";
    const index = characters.map(char => char.text).join("").indexOf(owner);
    expect(index).toBeGreaterThanOrEqual(0);
    for (const char of characters.slice(index, index + owner.length)) expect(char.bold).toBe(false);
    await io.send("\x1b[B");
    expect(io.bold(io.locate("OFF"))).toBe(false); expect(io.bold(io.locate(language === "zh" ? "中文" : "English"))).toBe(true);
  } finally {io.close();}
});

test("editing blinks only the field name, cycles one visible value and retains its column", async () => {
  const io = mount("zh");
  try {
    await wait(); const at = io.locate("详细显示"), value = io.locate("OFF");
    await io.send("\r"); expect(io.footer().hidden).toBe(true);
    await wait(450);
    expect(io.line(at.y)).not.toContain("详细显示"); expect(io.line(at.y)).toContain("OFF");
    expect(io.locate("OFF").x).toBe(value.x);
    expect(io.line(at.y)).not.toMatch(/[›▸█]/);
    await io.send("\x1b[C"); expect(io.line(at.y)).toContain("ON"); expect(io.line(at.y)).not.toContain("OFF");
    await io.send("\x1b[C"); expect(io.line(at.y)).toContain("OFF");
    expect(io.calls.some(call => call.name === "config.set")).toBe(false);
    await io.send("\r"); expect(io.line(at.y)).toContain("详细显示");
    expect(io.calls.find(call => call.name === "config.set")!.args).toEqual({key: "verbose", value: false});
  } finally {io.close();}
});
