import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import { Box, renderSync } from "@anthropic/ink";
import { TurnTimeline } from "./TurnTimeline.js";
import { LanguageContext } from "../ui/PageFrame.js";
import { runtimeNoticeFromEvent } from "../daemon/runtimeNotices.js";
import instances from "../../vendor/ink/src/core/instances.js";
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

async function renderTimeline(verbose: boolean, renderedTools = tools, openDetails=false,
  presentation: {language?:"en"|"zh";notices?:RuntimeNotice[]} = {},
): Promise<string> {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  stderr.on("data", () => {});
  const root = renderSync(
    <LanguageContext.Provider value={presentation.language || "en"}>
      <Box width={100}><TurnTimeline activity={activity} tools={renderedTools} notices={presentation.notices || notices}
        verbose={verbose} width={96}/></Box>
    </LanguageContext.Provider>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  await new Promise(resolve => setTimeout(resolve, 30));
  if(openDetails){
    const text=(node:any):string=>node.nodeValue ?? (node.childNodes||[]).map(text).join("");
    const nodes=(node:any):any[]=>[node,...(node.childNodes||[]).flatMap(nodes)];
    for(const label of ["write_file","Completion blocked"]){
      const target=nodes((instances.get(stdout) as any).rootNode).find(node=>node._eventHandlers?.onClick&&text(node).includes(label));
      expect(target).toBeDefined();target._eventHandlers.onClick({});
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  return stripAnsi(output);
}

describe("shared main/subprocess turn timeline renderer", () => {
  test("renders the helper's tool and status in the selected language", async () => {
    const terminal = runtimeNoticeFromEvent({event:"agent_terminal",process_id:"helper",
      role:"subagent",status:"completed"})!;
    const helperTool = {...tools[0]!,actor_label:"Subagent"};
    const english = await renderTimeline(false,[helperTool],false,{notices:[terminal]});
    const chinese = await renderTimeline(false,[helperTool],false,{language:"zh",notices:[terminal]});
    expect(english).toContain("Subagent · write_file");
    expect(english).toContain("Subagent completed");
    expect(chinese).toContain("子代理 · write_file");
    expect(chinese).toContain("子代理已完成");
    expect(chinese).toContain("子进程");
  });

  test("compact keeps progress and record headers while tool bodies and reasoning remain folded", async () => {
    const output = await renderTimeline(false);
    expect(output).toContain("Checked the existing file.");
    expect(output).toContain("write_file");
    expect(output).not.toContain("const value = 1;");
    expect(output).toContain("Completion blocked");
    expect(output).not.toContain("private chain");
  });

  test("verbose retains public progress and explicitly opened details without retaining historical thinking", async () => {
    expect(await renderTimeline(true)).not.toContain("const value = 1;");
    const output = await renderTimeline(true,tools,true);
    for (const expected of ["Checked the existing file.", "write_file", "const value = 1;", "Completion blocked"]) {
      expect(output).toContain(expected);
    }
    expect(output).not.toContain("private chain");
    expect(output).not.toContain("Thinking");
    expect(output).not.toContain("Progress");
  });

  test("uses one restrained detail indent beneath timeline headers", async () => {
    const output = await renderTimeline(true,tools,true);
    const lines = output.split(/\r?\n/);
    const leading = (needle: string) => {
      const line = lines.find(candidate => candidate.includes(needle));
      expect(line).toBeDefined();
      return line!.match(/^ */)![0].length;
    };
    const toolIndent = leading("write_file");
    const toolResultIndent = leading("saved");
    const noticeIndent = leading("Completion blocked");
    const noticeDetailIndent = leading("Required evidence missing");
    expect(leading("Checked the existing file.")).toBe(toolIndent);
    expect(toolResultIndent).toBe(toolIndent + 2);
    expect(noticeDetailIndent).toBe(noticeIndent + 2);
  });

  test("attaches usage only to current live thinking", async () => {
    const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
    stdin.ref = () => stdin; stdin.unref = () => stdin;
    const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
    const stderr: any = new PassThrough();
    let output = "";
    stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
    const root = renderSync(
      <Box width={100}><TurnTimeline activity={[]} tools={[]} notices={[]}
        verbose width={96} active thinking providerUsage={{input_tokens: 16486, output_tokens: 4901}}/></Box>,
      {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
    );
    await new Promise(resolve => setTimeout(resolve, 30));
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
    const plain = stripAnsi(output);
    expect(plain).toContain("Thinking…");
    expect(plain).toContain("16486 in / 4901 out");
    expect(plain).not.toContain("Provider usage");
    expect(plain).not.toContain("cache");
  });

  test("renders the compact unified diff emitted by write/edit tools", async () => {
    const output = await renderTimeline(true, [compactToolDiff],true);
    expect(output).toContain("result.py");
    expect(output).toContain("answer = 42");
  });

  test("collapses consecutive parallel web calls into one semantic row", async () => {
    const webTools: ToolCallCard[] = [0, 1, 2].map(index => ({
      tool_name: index === 0 ? "web_search" : "web_fetch",
      tool_call_id: `web-${index}`, target: `https://example.com/${index}`,
      allowed: true, status_label: "OK", duration_ms: 100 + index,
      timestamp: new Date(Date.parse("2026-11-08T00:00:00Z") + index * 10).toISOString(), state: "completed", result_text: `page ${index}`,
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
    expect(plain).toContain("Web research · 3 calls");
    expect(plain).not.toContain("https://example.com");
  });
});
