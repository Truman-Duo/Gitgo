import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import { Box, renderSync } from "@anthropic/ink";
import { TurnTimeline } from "./TurnTimeline.js";
import type { RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";

const activity: StreamActivity[] = [
  {kind: "reasoning", text: "private chain", process_id: "b", actor_label: "Subprocess"},
  {kind: "progress", text: "Checked the existing file.", visibility: "public", actor_label: "Subprocess"},
  {kind: "tool", toolIndex: 0},
  {kind: "notice", noticeIndex: 0},
];
const tools: ToolCallCard[] = [{
  tool_name: "write_file", tool_call_id: "write", target: "index.ts",
  allowed: true, status_label: "OK", duration_ms: 1010, timestamp: "",
  result_text: "saved", state: "completed", actor_label: "Subprocess",
  diff: "diff --git a/index.ts b/index.ts\n--- a/index.ts\n+++ b/index.ts\n@@ -0,0 +1 @@\n+const value = 1;\n",
}];
const compactToolDiff: ToolCallCard = {
  ...tools[0]!,
  tool_call_id: "compact-write",
  // This is the exact envelope produced by the production write_file tool.
  diff: "--- a/result.py\n+++ b/result.py\n@@ -0,0 +1 @@\n+answer = 42\n",
};
const notices: RuntimeNotice[] = [{
  key: "blocked", kind: "completion_gate", label: "Completion blocked",
  severity: "warning", actor_label: "Subprocess", detail: "Required evidence missing",
}];

async function renderTimeline(verbose: boolean, renderedTools = tools): Promise<string> {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  stderr.on("data", () => {});
  const root = renderSync(
    <Box width={100}><TurnTimeline activity={activity} tools={renderedTools} notices={notices}
      verbose={verbose} width={96}/></Box>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  await new Promise(resolve => setTimeout(resolve, 30));
  root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  return stripAnsi(output);
}

describe("shared main/subprocess turn timeline renderer", () => {
  test("compact keeps progress, tool, diff and notice but hides raw reasoning", async () => {
    const output = await renderTimeline(false);
    expect(output).toContain("Checked the existing file.");
    expect(output).toContain("write_file");
    expect(output).toContain("const value = 1;");
    expect(output).toContain("Completion blocked");
    expect(output).not.toContain("private chain");
  });

  test("verbose expands reasoning without removing the other timeline elements", async () => {
    const output = await renderTimeline(true);
    for (const expected of ["private chain", "Checked the existing file.", "write_file", "const value = 1;", "Completion blocked"]) {
      expect(output).toContain(expected);
    }
  });

  test("uses one restrained detail indent beneath timeline headers", async () => {
    const output = await renderTimeline(true);
    const lines = output.split(/\r?\n/);
    const leading = (needle: string) => {
      const line = lines.find(candidate => candidate.includes(needle));
      expect(line).toBeDefined();
      return line!.match(/^ */)![0].length;
    };
    const reasoningIndent = leading("private chain");
    const toolIndent = leading("write_file");
    const toolResultIndent = leading("⎿ saved");
    const noticeIndent = leading("Completion blocked");
    const noticeDetailIndent = leading("Required evidence missing");
    expect(reasoningIndent).toBe(toolIndent + 2);
    expect(toolResultIndent).toBe(toolIndent + 2);
    expect(noticeDetailIndent).toBe(noticeIndent + 2);
  });

  test("attaches usage to Thinking instead of creating a separate provider row", async () => {
    const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
    stdin.ref = () => stdin; stdin.unref = () => stdin;
    const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
    const stderr: any = new PassThrough();
    let output = "";
    stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
    const root = renderSync(
      <Box width={100}><TurnTimeline activity={[]} tools={[]} notices={[]}
        verbose={false} width={96} providerUsage={{input_tokens: 16486, output_tokens: 4901}}/></Box>,
      {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
    );
    await new Promise(resolve => setTimeout(resolve, 30));
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
    const plain = stripAnsi(output);
    expect(plain).toContain("Thinking · 16486 in / 4901 out");
    expect(plain).not.toContain("Provider usage");
    expect(plain).not.toContain("cache");
  });

  test("renders the compact unified diff emitted by write/edit tools", async () => {
    const output = await renderTimeline(false, [compactToolDiff]);
    expect(output).toContain("result.py");
    expect(output).toContain("answer = 42");
  });

  test("collapses consecutive parallel web calls into one semantic row", async () => {
    const webTools: ToolCallCard[] = [0, 1, 2].map(index => ({
      tool_name: index === 0 ? "web_search" : "web_fetch",
      tool_call_id: `web-${index}`, target: `https://example.com/${index}`,
      allowed: true, status_label: "OK", duration_ms: 100 + index,
      timestamp: "", state: "completed", result_text: `page ${index}`,
    }));
    const webActivity: StreamActivity[] = webTools.map((_, toolIndex) => ({kind: "tool", toolIndex}));
    const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
    stdin.ref = () => stdin; stdin.unref = () => stdin;
    const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
    const stderr: any = new PassThrough();
    let output = "";
    stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
    const root = renderSync(
      <Box width={100}><TurnTimeline activity={webActivity} tools={webTools} notices={[]}
        verbose width={96}/></Box>,
      {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
    );
    await new Promise(resolve => setTimeout(resolve, 30));
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
    const plain = stripAnsi(output);
    expect(plain).toContain("Web research (3)");
    expect(plain).not.toContain("https://example.com");
  });
});
