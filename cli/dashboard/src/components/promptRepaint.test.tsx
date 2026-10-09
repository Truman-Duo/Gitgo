import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { Box, Text, instances, renderSync } from "@anthropic/ink";
import { CommandBar } from "./CommandBar.js";
import { useTextInput, type UseTextInputReturn } from "../hooks/useTextInput.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 25));

function screenLines(ink: any): string[] {
  const screen = ink.frontFrame.screen;
  return Array.from({length: screen.height}, (_, y) => {
    let line = "";
    for (let x = 0; x < screen.width; x++) line += screen.charPool.get(screen.cells[(y * screen.width + x) * 2]);
    return line.trimEnd();
  });
}

for (const altScreen of [true, false]) {
  test(`prompt editor round-trip keeps cached content and caret synchronized (${altScreen ? "alternate" : "main"} screen)`, async () => {
    const stdin: any = new PassThrough();
    stdin.isTTY = true; stdin.setRawMode = () => stdin;
    stdin.ref = () => stdin; stdin.unref = () => stdin;
    const stdout: any = new PassThrough();
    stdout.isTTY = true; stdout.columns = 80; stdout.rows = 24;
    const stderr: any = new PassThrough();
    const writes: string[] = [];
    stdout.on("data", (chunk: Buffer) => writes.push(String(chunk)));
    stderr.on("data", () => {});
    let buffer!: UseTextInputReturn;
    function Fixture() {
      buffer = useTextInput();
      const command = useTextInput();
      return <Box flexDirection="column" height={24} width={80}>
        <Box flexGrow={1}><Text>Conversation remains visible</Text></Box>
        <CommandBar width={80} mode="NORMAL" scene="workspace" textInput={buffer} cmdInput={command}
          cmdResult="" statusText="Ready" suggestions={[]} suggestionIdx={0}/>
      </Box>;
    }
    const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
    const ink = instances.get(stdout)!;
    try {
      ink.setAltScreenActive(altScreen);
      ink.onRender();
      await wait();
      expect(screenLines(ink).join("\n")).toContain("Conversation remains visible");
      // Exercise the actual renderer cache reset, not just a mocked method order.
      ink.pause();
      writes.length = 0;
      ink.repaint();
      if (altScreen) expect((ink as any).frontFrame.screen.height).toBe(24);
      ink.resume();
      expect(screenLines(ink).join("\n")).toContain("Conversation remains visible");
      if (altScreen) expect(writes.join("")).not.toContain("\n");
      for (const draft of ["短文本", "line one\r\n第二行", "长文本甲乙丙丁".repeat(12)]) {
        ink.pause();
        ink.suspendStdin();
        buffer.setExternalValue(draft);
        await wait();
        ink.resume({redraw: true});
        ink.resumeStdin();
        ink.onRender();
        expect(buffer.value).toBe(draft.replace(/\r\n/g, "\n"));
        const lines = screenLines(ink);
        expect(lines.join("\n")).toContain("Conversation remains visible");
        expect(lines.filter(line => line.includes("NORMAL")).length).toBe(1);
        const caret = (ink as any).displayCursor;
        expect(caret).not.toBeNull();
        expect(caret.y).toBeGreaterThanOrEqual(0);
        expect(caret.y).toBeLessThan(24);
        // Editing and clearing (the send path) must not require a resize.
        buffer.insertText("!");
        await wait();
        expect(buffer.value.endsWith("!")).toBeTrue();
        buffer.setValue("");
        await wait();
        const cleared = screenLines(ink);
        expect(cleared.join("\n")).toContain("Conversation remains visible");
        expect(cleared.filter(line => line.includes("NORMAL")).length).toBe(1);
      }
    } finally {
      root.unmount(); root.cleanup();
      stdin.destroy(); stdout.destroy(); stderr.destroy();
    }
  });
}
