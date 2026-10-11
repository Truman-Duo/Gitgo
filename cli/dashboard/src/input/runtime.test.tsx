import React from "react";
import { test } from "node:test";
import assert from "node:assert/strict";
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
  const keyEvents: any[] = [];
  const rawModes: boolean[] = [];
  stdin.setRawMode = (enabled: boolean) => {rawModes.push(enabled); return stdin;};
  let modal = false;
  function Probe() {
    useManagedInput((_input, key) => { if (!modal) return false; events.push(key.tab ? "modal-tab" : "modal"); }, {priority: 100});
    useManagedInput((input, key) => {
      keyEvents.push({input, key});
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
    assert.deepStrictEqual(events, ["scene-tab", "modal-tab"]);
    modal = false;
    const unicode = Buffer.from("中文𠮷", "utf8");
    for (const byte of unicode) stdin.write(Buffer.from([byte]));
    await new Promise(resolve => setTimeout(resolve, 25));
    assert.strictEqual(textEvents.join(""), "中文𠮷");
    const sleep = () => new Promise(resolve => setTimeout(resolve, 25));
    stdin.write("\x1b[13;2u"); // Shift+Enter
    await sleep();
    assert.strictEqual(keyEvents.at(-1).key.shift, true);
    assert.strictEqual(keyEvents.at(-1).key.return, true);
    stdin.write("\x07"); // Ctrl+G
    await sleep();
    assert.strictEqual(keyEvents.at(-1).input, "g");
    assert.strictEqual(keyEvents.at(-1).key.ctrl, true);
    stdin.write("\x1b[200~中文\n\x03literal\x1b[201~");
    await sleep();
    assert.strictEqual(keyEvents.at(-1).input, "中文\n\x03literal");
    assert.strictEqual(keyEvents.at(-1).key.paste, true);
    const count = keyEvents.length;
    stdin.write("\x1b[99;9u"); // Cmd+C via kitty protocol is consumed as copy.
    await sleep();
    assert.strictEqual(keyEvents.length, count);
    stdin.write("\x03"); // Ctrl+C must not reach task cancellation/scene handlers.
    await new Promise(resolve => setTimeout(resolve, 25));
    assert.deepStrictEqual(events, ["scene-tab", "modal-tab"]);
  } finally {
    instance.unmount(); instance.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
  assert.strictEqual(rawModes[0], true);
  assert.strictEqual(rawModes.at(-1), false);
});
