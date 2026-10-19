import React from "react";
import { test, expect } from "bun:test";
import { PassThrough } from "node:stream";
import { Box, renderSync } from "@anthropic/ink";
import stripAnsi from "strip-ansi";
import { InputProvider } from "../input/runtime.js";
import { useTextInput } from "../hooks/useTextInput.js";
import { StatsPanel } from "./StatsPanel.js";
import { StatsOverview } from "./StatsOverview.js";
import { BinWorkspace } from "./BinWorkspace.js";
import type { BackendClient } from "../backend/client.js";
function terminal() {
  const stdin: any = new PassThrough();
  stdin.isTTY = true;
  stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin;
  stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = false;
  stdout.columns = 120;
  stdout.rows = 36;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (c: Buffer) => {
    output += c.toString();
  });
  stderr.on("data", () => {});
  return { stdin, stdout, stderr, text: () => stripAnsi(output) };
}
const wait = () => new Promise((r) => setTimeout(r, 90));
test("stats loads bounded timing pages and can inspect an older turn without mixing traces", async () => {
  const io = terminal();
  const calls: any[] = [];
  const client: BackendClient = {
    ready: true,
    close() {},
    async callTool(operation, args = {}) {
      calls.push({ operation, args });
      if (operation === "runtime.usage")
        return {
          tasks: [
            {
              task_id: "task",
              status: "completed",
              provider_calls: 1,
              tool_calls: 0,
            },
          ],
          page: { has_more: false },
        };
      return args.after_seq
        ? {
            events: [
              {
                seq: 2,
                event: "provider_response_completed",
                process_id: "a",
                time: "2026-10-17T00:00:02Z",
                duration_ms: 2000,
              },
            ],
            has_more: false,
            next_seq: 2,
          }
        : {
            events: [
              {
                seq: 1,
                event: "provider_request_started",
                process_id: "a",
                step: 1,
                time: "2026-10-17T00:00:00Z",
              },
            ],
            has_more: true,
            next_seq: 1,
          };
    },
  };
  const root = renderSync(
    <InputProvider>
      <Box height={36} flexDirection="column">
        <StatsPanel
          client={client}
          project="demo"
          cols={120}
          onDismiss={() => {}}
        />
      </Box>
    </InputProvider>,
    { ...io, patchConsole: false, exitOnCtrlC: false },
  );
  try {
    await wait();
    expect(io.text()).toContain("Turn waterfall");
    expect(io.text()).toContain("partial trace");
    io.stdin.write("l");
    await wait();
    expect(calls).toContainEqual({
      operation: "runtime.trace",
      args: {
        project: "demo",
        action: "read",
        trace_id: "task",
        after_seq: 1,
        limit: 1000,
        include_deltas: false,
      },
    });
    expect(io.text()).toContain("2 retained events");
  } finally {
    root.unmount();
    root.cleanup();
    io.stdin.destroy();
    io.stdout.destroy();
    io.stderr.destroy();
  }
});
test("global stats does not show queried zero totals before the query completes", async () => {
  const io = terminal();
  let resolve!: (v: any) => void;
  const client: BackendClient = {
    ready: true,
    close() {},
    callTool: () =>
      new Promise((r) => {
        resolve = r;
      }),
  };
  const root = renderSync(
    <InputProvider>
      <StatsOverview client={client} cols={120} onDismiss={() => {}} />
    </InputProvider>,
    { ...io, patchConsole: false, exitOnCtrlC: false },
  );
  try {
    await wait();
    expect(io.text()).toContain("Loading authoritative usage");
    expect(io.text()).not.toContain("0 tokens");
    resolve({
      summary: { task_count: 0, provider_calls: 0 },
      daily: [],
      tasks: [],
      projects: [],
      complete: true,
    });
    await wait();
    expect(io.text()).toContain("USD cost is unrecorded");
  } finally {
    root.unmount();
    root.cleanup();
    io.stdin.destroy();
    io.stdout.destroy();
    io.stderr.destroy();
  }
});
test("bin owns the missing-workspace delay, and cancelling its draft never saves it", async () => {
  const io = terminal();
  const calls: any[] = [];
  const client: BackendClient = {
    ready: true,
    close() {},
    async callTool(operation, args = {}) {
      calls.push({ operation, args });
      if (operation === "config.get")
        return { safety: { unavailable_project_archive_delay_minutes: 60 } };
      return {};
    },
  };
  function Fixture() {
    const input = useTextInput();
    return (
      <BinWorkspace
        client={client}
        cmdInput={input}
        onFooter={() => {}}
        onDismiss={() => {}}
      />
    );
  }
  const root = renderSync(
    <InputProvider>
      <Fixture />
    </InputProvider>,
    { ...io, patchConsole: false, exitOnCtrlC: false },
  );
  try {
    await wait();
    for (let i = 0; i < 2; i++) {
      io.stdin.write("\x1b[C");
      await wait();
    }
    for (let i = 0; i < 2; i++) {
      io.stdin.write("\x1b[B");
      await wait();
    }
    expect(io.text()).toContain("Missing workspace archive");
    io.stdin.write("\r");
    await wait();
    io.stdin.write("\x1b[C");
    await wait();
    io.stdin.write("\x1b");
    await wait();
    expect(calls.some((c) => c.operation === "config.set")).toBe(false);
    io.stdin.write("\r");
    await wait();
    io.stdin.write("\x1b[C");
    await wait();
    io.stdin.write("\r");
    await wait();
    expect(calls).toContainEqual({
      operation: "config.set",
      args: {
        key: "safety.unavailable_project_archive_delay_minutes",
        value: 1440,
      },
    });
  } finally {
    root.unmount();
    root.cleanup();
    io.stdin.destroy();
    io.stdout.destroy();
    io.stderr.destroy();
  }
});
