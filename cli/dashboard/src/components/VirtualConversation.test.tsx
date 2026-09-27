import React, {createRef} from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync, useTerminalSize} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import type {ChatMessage} from "../types.js";
import {VirtualConversation} from "./VirtualConversation.js";

function mountedText(node: any): string {
  if (node?.nodeName === "#text") return String(node.nodeValue || "");
  return (node?.childNodes || []).map(mountedText).join("");
}

test("conversation mounts a moving message window instead of the full history", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 90; stdout.rows = 16;
  const stderr: any = new PassThrough(); stdout.on("data", () => {}); stderr.on("data", () => {});
  const scrollRef = createRef<ScrollBoxHandle>();
  const messages: ChatMessage[] = Array.from({length: 1000}, (_, index) => ({
    role: "assistant",
    content: `VIRTUAL_ROW_${index}`,
    timestamp: `2026-09-15T00:00:${String(index).padStart(2, "0")}Z`,
    message_id: `message-${index}`,
  }));
  const root = renderSync(
    <Box flexDirection="column" width={90} height={16}>
      <VirtualConversation messages={messages} streaming={null} contentWidth={86} scrollRef={scrollRef}/>
    </Box>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  const settle = () => new Promise(resolve => setTimeout(resolve, 80));
  try {
    await settle();
    let text = mountedText((instances.get(stdout) as any).rootNode);
    expect(text).toContain("VIRTUAL_ROW_0");
    expect(text).not.toContain("VIRTUAL_ROW_999");
    expect((text.match(/VIRTUAL_ROW_/g) || []).length).toBeLessThan(40);

    scrollRef.current?.scrollToBottom();
    expect(scrollRef.current?.isSticky()).toBe(true);
    await settle();
    text = mountedText((instances.get(stdout) as any).rootNode);
    expect(text).toContain("VIRTUAL_ROW_999");
    expect(text).not.toContain("VIRTUAL_ROW_0");
    expect((text.match(/VIRTUAL_ROW_/g) || []).length).toBeLessThan(40);
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("virtualized width reflow preserves a semantic mid-history anchor", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 18;
  const stderr: any = new PassThrough(); stdout.on("data", () => {}); stderr.on("data", () => {});
  const scrollRef = createRef<ScrollBoxHandle>();
  const messages: ChatMessage[] = Array.from({length: 180}, (_, index) => ({
    role: "assistant",
    content: `REFLOW_ANCHOR_${index} ${"wrapping content ".repeat(5)}`,
    timestamp: `2026-09-15T00:00:${String(index).padStart(2, "0")}Z`,
    message_id: `reflow-${index}`,
  }));
  function Fixture() {
    const {columns} = useTerminalSize();
    const width = columns || stdout.columns;
    return <Box flexDirection="column" width="100%" height={18}>
      <VirtualConversation messages={messages} streaming={null}
        contentWidth={width - 4} scrollRef={scrollRef}/>
    </Box>;
  }
  const root = renderSync(<Fixture/>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const settle = () => new Promise(resolve => setTimeout(resolve, 100));
  const ids = () => [...mountedText((instances.get(stdout) as any).rootNode)
    .matchAll(/REFLOW_ANCHOR_(\d+)/g)].map(match => Number(match[1]));
  try {
    await settle();
    scrollRef.current?.scrollTo(360);
    await settle();
    const before = ids();
    expect(before.length).toBeGreaterThan(0);
    const semanticAnchor = before[Math.floor(before.length / 2)]!;
    expect(scrollRef.current?.isSticky()).toBe(false);

    stdout.columns = 56;
    stdout.emit("resize");
    await settle();
    const after = ids();
    expect(after).toContain(semanticAnchor);
    expect(after).not.toContain(179);
    expect(scrollRef.current?.isSticky()).toBe(false);
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});
