import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { renderSync, Text } from "@anthropic/ink";
import { InputProvider, useManagedInput } from "./runtime.js";

// In-memory renderer/input integration, NOT visual acceptance of a real terminal.
test("Ink stdin reaches exactly one registered scene/modal owner", async () => {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = false; stdout.columns = 100; stdout.rows = 24;
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString(); });
  const stderr: any = new PassThrough();
  stderr.on("data", () => {});
  const events: string[] = [];
  const textEvents: string[] = [];
  let modal = false;
  function Probe() {
    useManagedInput((_input, key) => { if (!modal) return false; events.push(key.tab ? "modal-tab" : "modal"); }, {priority: 100});
    useManagedInput((input, key) => {
      if (key.tab) events.push("scene-tab");
      else if (input) textEvents.push(input);
    });
    return <Text>Input control test</Text>;
  }
  const instance = renderSync(<InputProvider><Probe /></InputProvider>,
    {stdin, stdout, stderr, exitOnCtrlC: false, patchConsole: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 50));
    stdin.write("\t");
    await new Promise(resolve => setTimeout(resolve, 25));
    modal = true;
    stdin.write("\t");
    await new Promise(resolve => setTimeout(resolve, 25));
    if (!events.length) throw new Error(`Renderer did not dispatch input: ${output}`);
    expect(events).toEqual(["scene-tab", "modal-tab"]);
    modal = false;
    const unicode = Buffer.from("中文𠮷", "utf8");
    for (const byte of unicode) stdin.write(Buffer.from([byte]));
    await new Promise(resolve => setTimeout(resolve, 25));
    expect(textEvents.join("")).toBe("中文𠮷");
    stdin.write("\x03"); // Ctrl+C must not reach task cancellation/scene handlers.
    await new Promise(resolve => setTimeout(resolve, 25));
    expect(events).toEqual(["scene-tab", "modal-tab"]);
  } finally {
    instance.unmount(); instance.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});
