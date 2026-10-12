import React from "react";
import {describe, expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";
import {UpdatePanel} from "./UpdatePanel.js";

const sample = {schema_version: 1, product_id: "gitgo-terminal", version: "1.0.0-alpha.2",
  status: "disabled", enabled: false, automatic_check: false, channel: "stable", can_apply: false,
  blockers: ["MANAGED_INSTALLATION_REQUIRED"]};
const wait = () => new Promise(resolve => setTimeout(resolve, 45));

function mount(client: BackendClient, onDismiss: () => void) {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin; stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 80; stdout.rows = 24;
  const stderr = new PassThrough();
  let text = "";
  stdout.on("data", (chunk: Buffer) => {text += chunk.toString();}); stderr.on("data", () => {});
  const root = renderSync(<InputProvider><UpdatePanel client={client} cols={80} onDismiss={onDismiss}/></InputProvider>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {stdin, text: () => text, close() {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }};
}

describe("software update panel", () => {
  test("reads local status, ignores Enter/apply, refreshes with R and exits with Escape", async () => {
    const calls: any[] = []; let dismissed = 0;
    const fixture = mount({ready: true, close() {}, async callTool(...args) {calls.push(args); return sample;}},
      () => {dismissed++;});
    try {
      await wait();
      expect(fixture.text()).toContain("1.0.0-alpha.2");
      expect(fixture.text()).toContain("Disabled");
      fixture.stdin.write("\r"); await wait();
      expect(calls).toEqual([["update.status"]]);
      fixture.stdin.write("r"); await wait();
      expect(calls).toEqual([["update.status"], ["update.status"]]);
      fixture.stdin.write("\x1b");
      // The real byte parser deliberately buffers lone Escape for 50ms to
      // distinguish it from an arrow/paste sequence.
      await new Promise(resolve => setTimeout(resolve, 100));
      expect(dismissed).toBe(1);
    } finally {fixture.close();}
  });
  test("transport failure is retryable without printing raw sensitive errors", async () => {
    let attempts = 0;
    const fixture = mount({ready: true, close() {}, async callTool() {
      attempts++; if (attempts === 1) throw new Error("proxy-password-must-not-appear"); return sample;
    }}, () => {});
    try {
      await wait();
      expect(fixture.text()).toContain("Update status unavailable");
      expect(fixture.text()).not.toContain("proxy-password-must-not-appear");
      expect(fixture.text()).not.toContain("Disabled");
      fixture.stdin.write("r"); await wait();
      expect(fixture.text()).toContain("Disabled");
    } finally {fixture.close();}
  });
  test("Escape works while status is pending, and late completion after unmount is ignored", async () => {
    let resolve!: (v: unknown) => void; let dismissed = 0;
    const fixture = mount({ready: true, close() {}, callTool: () => new Promise(r => {resolve = r;})},
      () => {dismissed++;});
    try {
      await wait(); fixture.stdin.write("r"); fixture.stdin.write("\x1b");
      await new Promise(r => setTimeout(r, 100));
      expect(dismissed).toBe(1);
    } finally {fixture.close();}
    resolve(sample); await wait();
  });
});
