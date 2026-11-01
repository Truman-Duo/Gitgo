import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Parser} from "../../vendor/ink/src/core/termio.js";
import {StylePool} from "../../vendor/ink/src/core/screen.js";
import {writeDiffToTerminal} from "../../vendor/ink/src/core/terminal.js";
import {optimize} from "../../vendor/ink/src/core/optimizer.js";
import type {AnsiCode} from "@alcalzone/ansi-tokenize";

const bold: AnsiCode = {type: "ansi", code: "\x1b[1m", endCode: "\x1b[22m"};
const dim: AnsiCode = {type: "ansi", code: "\x1b[2m", endCode: "\x1b[22m"};
const foreground: AnsiCode = {type: "ansi", code: "\x1b[38;2;240;240;240m", endCode: "\x1b[39m"};

test("every intensity transition preserves the requested bold/dim state", () => {
  const pool = new StylePool();
  const states = [[], [bold], [dim], [bold, dim]].flatMap(state => [state, [...state, foreground]]);
  for (const from of states) for (const to of states) {
    const parser = new Parser();
    const sequence = from.map(code => code.code).join("") + pool.transition(pool.intern(from), pool.intern(to)) + "X";
    const text = parser.feed(sequence).find(action => action.type === "text");
    if (text?.type !== "text") throw Error("No text from transition");
    expect({from, to, bold: text.style.bold, dim: text.style.dim}).toEqual({from, to,
      bold: to.some(code => code.code === bold.code), dim: to.some(code => code.code === dim.code)});
  }
});

test("a terminal frame establishes neutral rendition after startup or another writer", () => {
  const stdout: any = new PassThrough(); stdout.isTTY = true;
  const parser = new Parser(); let output = "";
  stdout.on("data", (bytes: Buffer) => {output += bytes.toString();});
  try {
    for (const preceding of ["\x1b[1m", "\x1b[2m", "\x1b[0m"]) {
      parser.feed(preceding); output = "";
      writeDiffToTerminal({stdout, stderr: stdout}, optimize([
        {type: "stdout", content: "plain"}, {type: "styleStr", str: bold.code},
        {type: "stdout", content: "selected"},
      ]));
      const segments = parser.feed(output).filter(action => action.type === "text");
      expect(segments.map(segment => ({text: segment.graphemes.map(g => g.value).join(""), bold: segment.style.bold, dim: segment.style.dim})))
        .toEqual([{text: "plain", bold: false, dim: false}, {text: "selected", bold: true, dim: false}]);
      expect(parser.style.bold).toBe(false); expect(parser.style.dim).toBe(false);
    }
    output = ""; writeDiffToTerminal({stdout, stderr: stdout}, []); expect(output).toBe("");
  } finally {stdout.destroy();}
});

test("plain piped output does not gain terminal control sequences", () => {
  const stdout: any = new PassThrough(); stdout.isTTY = false; let output = "";
  stdout.on("data", (bytes: Buffer) => {output += bytes.toString();});
  try {
    writeDiffToTerminal({stdout, stderr: stdout}, [{type: "stdout", content: "plain"}], true);
    expect(output).toBe("plain");
  } finally {stdout.destroy();}
});
