import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { renderSync } from "@anthropic/ink";
import type { BackendClient } from "../../backend/client.js";
import { useTextInput } from "../../hooks/useTextInput.js";
import { InputProvider } from "../../input/runtime.js";
import { GeneralTab } from "./GeneralTab.js";

function terminalStreams() {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = false; stdout.columns = 100; stdout.rows = 30;
  const stderr: any = new PassThrough();
  stdout.on("data", () => {}); stderr.on("data", () => {});
  return {stdin, stdout, stderr};
}

describe("General network configuration", () => {
  test("saves an engine choice and an endpoint through the shared input router", async () => {
    const calls: Array<{operation: string; args: Record<string, unknown>}> = [];
    const client: BackendClient = {
      ready: true,
      callTool: async (operation, args = {}) => {
        calls.push({operation, args});
        if (operation === "config.get") return {
          verbose: false, language: "en", auto_compact: true,
          agent_routing: "owner", external_editor: "",
          web_search_mode: "auto",
          web_search_engine: "duckduckgo",
          web_search_endpoint: "https://search.example.test/search",
        };
        if (operation === "config.web_search.test") return {reachable: true};
        return {ok: true};
      },
      close: () => {},
    };
    const streams = terminalStreams();
    function Fixture() {
      const input = useTextInput();
      return <InputProvider><GeneralTab
        client={client} project="test" cmdInput={input}
        onFooter={() => {}} report={() => {}}
        shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
        contentFocused
      /></InputProvider>;
    }
    const root = renderSync(<Fixture/>, {...streams, patchConsole: false, exitOnCtrlC: false});
    const wait = () => new Promise(resolve => setTimeout(resolve, 35));
    try {
      await wait();
      // Fallback engine follows the Web Search policy row. Enter opens the finite selector;
      // right moves DuckDuckGo to Google and Enter persists it.
      for (let index = 0; index < 6; index++) { streams.stdin.write("\x1b[B"); await wait(); }
      streams.stdin.write("\r"); await wait();
      streams.stdin.write("\x1b[C"); await wait();
      streams.stdin.write("\r"); await wait();
      expect(calls).toContainEqual({
        operation: "config.set",
        args: {key: "web_search_engine", value: "google"},
      });

      // The endpoint uses the NormalBar, but Enter is still owned by this
      // form and must persist rather than escaping the Config scene.
      streams.stdin.write("\x1b[B"); await wait();
      streams.stdin.write("\r"); await wait();
      streams.stdin.write("\r"); await wait();
      expect(calls).toContainEqual({
        operation: "config.set",
        args: {key: "web_search_endpoint", value: "https://search.example.test/search"},
      });
      expect(calls.filter(item => item.operation === "config.web_search.test").length).toBeGreaterThanOrEqual(2);
    } finally {
      root.unmount(); root.cleanup();
      streams.stdin.destroy(); streams.stdout.destroy(); streams.stderr.destroy();
    }
  });
});

describe("General terminal choices", () => {
  for (const firstLaunch of [true, false]) test(`${firstLaunch ? "first launch" : "later General"} selects a discovered terminal using arrow keys`, async () => {
    const saved: any[] = [];
    const notices: string[] = [];
    let scans = 0;
    const client: BackendClient = {ready: true, close() {}, async callTool(operation, args = {}) {
      if (operation === "config.get") return {launcher: {terminal: "auto"}};
      if (operation === "config.terminals") {
        scans++;
        return {options: [
          {id: "auto", label: "Automatic", available: true},
          {id: "current", label: "Current terminal", available: true},
          {id: "fake_bash", label: "Fake Bash", available: false},
          {id: "git_bash", label: "Discovered Git Bash", available: true},
        ], warnings: ["Fake Bash rejected: invalid signature"]};
      }
      if (operation === "config.set") { saved.push(args); return {ok: true}; }
      return {};
    }};
    const streams = terminalStreams();
    let output = "";
    streams.stdout.on("data", (data: Buffer) => { output += data.toString(); });
    function Fixture() {
      const input = useTextInput();
      return <InputProvider><GeneralTab client={client} project="test" cmdInput={input}
        onFooter={() => {}} report={() => {}} contentFocused
        initialSetting={firstLaunch ? "terminal" : undefined}
        onSettingSaved={async key => { notices.push(key); return "Terminal is ready"; }}
        shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      /></InputProvider>;
    }
    const root = renderSync(<Fixture/>, {...streams, patchConsole: false, exitOnCtrlC: false});
    const wait = () => new Promise(resolve => setTimeout(resolve, 40));
    try {
      await wait();
      if (!firstLaunch) for (let i = 0; i < 8; i++) { streams.stdin.write("\x1b[B"); await wait(); }
      streams.stdin.write("r"); await wait();
      streams.stdin.write("\x1b[C"); await wait();
      streams.stdin.write("\x1b[C"); await wait();
      streams.stdin.write("\r"); await wait(); await wait();
      expect(saved).toEqual([{key: "launcher.terminal", value: "git_bash"}]);
      expect(notices).toEqual(["launcher.terminal"]);
      expect(scans).toBe(2);
      expect(output).toContain("invalid signature");
    } finally {
      root.unmount(); root.cleanup();
      streams.stdin.destroy(); streams.stdout.destroy(); streams.stderr.destroy();
    }
  });
});
