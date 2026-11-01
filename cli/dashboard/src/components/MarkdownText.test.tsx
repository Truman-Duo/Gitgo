import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import chalk from "chalk";
import { Box, renderSync } from "@anthropic/ink";
import { MarkdownText, StreamingMarkdownText } from "./MarkdownText.js";
import {displayWidth} from "../theme/typography.js";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";

async function renderMarkdown(content: string, width: number, dimColor = false, streaming = false, raw = false): Promise<string> {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = width; stdout.rows = 80;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  const Component = streaming ? StreamingMarkdownText : MarkdownText;
  const root = renderSync(
    <Box width={width}><Component content={content} width={width} dimColor={dimColor}/></Box>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  await new Promise(resolve => setTimeout(resolve, 20));
  const screen = (instances.get(stdout) as any).frontFrame.screen;
  const frame = Array.from({length: screen.height}, (_, y) =>
    Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? " ").join("").trimEnd(),
  ).join("\n");
  root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  return raw ? output : frame;
}

// Inspect emitted terminal foregrounds, rather than asserting component props
// or stripping away the very color contract this regression protects.
function foregroundAt(output: string, needle: string): string | undefined {
  let foreground: string | undefined;
  let plain = "";
  const foregrounds: (string | undefined)[] = [];
  for (const part of output.split(/(\x1b\[[0-9;]*m)/)) {
    if (part.startsWith("\x1b[")) {
      const codes = part.slice(2, -1).split(";").map(Number);
      for (let index = 0; index < codes.length; index++) {
        if (codes[index] === 0 || codes[index] === 39) foreground = undefined;
        if (codes[index] === 38 && codes[index + 1] === 2) {
          foreground = codes.slice(index + 2, index + 5).join(",");
          index += 4;
        }
      }
    } else {
      const text = stripAnsi(part);
      plain += text;
      foregrounds.push(...Array.from({length: text.length}, () => foreground));
    }
  }
  const offset = plain.indexOf(needle);
  expect(offset).toBeGreaterThanOrEqual(0);
  return foregrounds[offset];
}

const chineseTable = [
  "| 项目 | 数据 |",
  "| --- | --- |",
  "| 型号 | be quiet! Dark Rock Pro 5 |",
  "| 总高度 | 168 毫米，安装前应核对机箱限高 |",
  "| 兼容性 | 支持主流桌面平台并需检查内存净空 |",
].join("\n");

describe("terminal markdown tables", () => {
  test("uses the chat emphasis color for inline code in completed and streaming messages", async () => {
    const previous = chalk.level;
    chalk.level = 3;
    try {
      for (const streaming of [false, true]) {
        const output = await renderMarkdown("Normal `HighlightedCode` text", 70, false, streaming, true);
        expect(foregroundAt(output, "HighlightedCode")).toBe("203,168,127");
        expect(foregroundAt(output, "Normal")).toBe("240,240,240");
      }
    } finally {
      chalk.level = previous;
    }
  });
  test("emits bright body and muted reasoning across nested Markdown and streaming blocks", async () => {
    const previous = chalk.level;
    chalk.level = 3;
    try {
      const content = "# Heading\n\nPlain **Strong** *Emphasis* [Link](https://example.com)\n\n- ListItem\n\n| Label | Value |\n| --- | --- |\n| Row | TableCell |\n\nLastBlock";
      for (const streaming of [false, true]) {
        for (const muted of [false, true]) {
          const output = await renderMarkdown(content, 70, muted, streaming, true);
          for (const needle of ["Heading", "Plain", "Strong", "Emphasis", "Link", "ListItem", "TableCell", "LastBlock"]) {
            expect(foregroundAt(output, needle)).toBe(muted ? "168,168,168" : "240,240,240");
          }
        }
      }
    } finally {
      chalk.level = previous;
    }
  });
  test("uses terminal cell width and wraps CJK cells without destructive ellipsis", async () => {
    const output = await renderMarkdown(chineseTable, 60);
    for (const text of ["项目", "数据", "型号", "总高度", "兼容性", "168 毫米", "内存净空"]) {
      expect(output).toContain(text);
    }
    expect(output).not.toContain("项…");
    expect(output).not.toContain("数…");
    const topRule = output.split("\n").find(line => line.startsWith("┌"));
    expect(topRule?.length).toBe(60);
  });

  test("keeps a narrow CJK table as a bounded grid with wrapped cells", async () => {
    const output = await renderMarkdown(chineseTable, 18);
    expect(output).toContain("项目");
    expect(output).toContain("型号");
    expect(output).toContain("┌");
    expect(output).toContain("└");
    expect(output).not.toContain("项目:");
    for (const line of output.trimEnd().split("\n")) expect(displayWidth(line)).toBeLessThanOrEqual(18);
    expect(output).not.toContain("项…");
  });

  test("keeps more than four columns in a wide table and retains all narrow column groups", async () => {
    const source = "| C1 | C2 | C3 | C4 | C5 | C6 |\n|---|---|---|---|---|---|\n|甲|乙|丙|丁|戊|己|";
    const wide = await renderMarkdown(source, 70);
    expect(wide.split("\n").some(line => ["C1", "C2", "C3", "C4", "C5", "C6"].every(value => line.includes(value)))).toBe(true);
    for (const width of [8, 14, 18]) {
      const narrow = await renderMarkdown(source, width);
      for (const value of ["甲", "乙", "丙", "丁", "戊", "己"]) expect(narrow).toContain(value);
      expect(narrow.split("┌").length).toBeGreaterThan(2);
      expect(narrow.split("甲").length).toBeGreaterThan(2);
      for (const line of narrow.trimEnd().split("\n")) expect(displayWidth(line)).toBeLessThanOrEqual(width);
    }
  });
});
