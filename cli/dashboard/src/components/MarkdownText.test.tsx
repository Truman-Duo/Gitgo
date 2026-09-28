import React from "react";
import { describe, expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import { Box, renderSync } from "@anthropic/ink";
import { MarkdownText } from "./MarkdownText.js";

async function renderMarkdown(content: string, width: number): Promise<string> {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = width; stdout.rows = 80;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  const root = renderSync(
    <Box width={width}><MarkdownText content={content} width={width}/></Box>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  await new Promise(resolve => setTimeout(resolve, 20));
  root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  return stripAnsi(output);
}

const chineseTable = [
  "| 项目 | 数据 |",
  "| --- | --- |",
  "| 型号 | be quiet! Dark Rock Pro 5 |",
  "| 总高度 | 168 毫米，安装前应核对机箱限高 |",
  "| 兼容性 | 支持主流桌面平台并需检查内存净空 |",
].join("\n");

describe("terminal markdown tables", () => {
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
