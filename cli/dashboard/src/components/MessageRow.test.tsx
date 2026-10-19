import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import { Box, renderSync } from "@anthropic/ink";
import { MessageRow } from "./MessageRow.js";
import type { BackendClient } from "../backend/client.js";

test("failed round keeps its error and reads cleanup evidence through the existing trace client", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr = new PassThrough();
  let output = "";
  const calls: any[] = [];
  stdout.on("data", chunk => { output += chunk.toString(); });
  const client = {callTool: async (name: string, args: any) => {
    calls.push({name, args});
    return {events: [
      {seq: 1, time: "now", event: "tool_result", process_id: "p", tool_name: "read_file",
        tool_call_id: "read", is_error: true, error: "cancelled", duration_ms: 10},
      {seq: 2, time: "now", event: "progress_summary", phase: "tool_notice", process_id: "p",
        code: "PROCESS_CLEANUP_INCOMPLETE", message: "Cleanup remains unknown; retain resource ownership."},
    ], has_more: false};
  }} as unknown as BackendClient;
  const root = renderSync(<Box width={100}><MessageRow msg={{
    role: "assistant", content: "[Error: cancelled]", timestamp: "now", kind: "error",
    status: "cancelled", trace_id: "trace", process_id: "p",
  }} contentWidth={96} project="fixture" client={client} expanded/></Box>,
  {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 50));
    const plain = stripAnsi(output);
    expect(plain).toContain("Failure details");
    expect(plain).toContain("[Error: cancelled]");
    expect(plain).toContain("read_file");
    expect(plain).toContain("Cleanup remains unknown");
    expect(plain).not.toContain("Completed");
    expect(calls).toHaveLength(1);
    expect(calls[0].name).toBe("runtime.trace");
    expect(calls[0].args.trace_id).toBe("trace");
    expect(calls[0].args.process_id).toBe("p");
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});
