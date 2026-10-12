import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import chalk from "chalk";
import { Box, renderSync } from "@anthropic/ink";
import { MarkdownText, StreamingMarkdownText } from "./MarkdownText.js";

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
  root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  return raw ? output : stripAnsi(output);
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

  test("degrades a narrow grid to readable labelled rows", async () => {
    const output = await renderMarkdown(chineseTable, 18);
    expect(output).toContain("项目: 型号");
    expect(output).toContain("数据: be quiet!");
    expect(output).toContain("项目: 总高度");
    expect(output).not.toContain("项…");
  });
});
