import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";

test("full App gives Publish its own viewport so long evidence cannot scroll away the plan header", async () => {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 100, rows: 28}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const calls: string[] = [];
  const client: BackendClient = {ready: true, close() {}, async callTool(op, args = {}) {
    calls.push(op);
    if (op === "config.get") return {language: "en", verbose: false, auto_compact: true};
    if (op === "project.overview") return {projects: [{name: "publish-layout", workspace: "C:/fixture", runtime_summary: {state_available: true}}]};
    if (op === "project.enter") return {ok: true, name: "publish-layout"};
    if (op === "runtime.status") return {state_available: true, daemon_online: false, processes: {}};
    if (op === "publish.items") return {schema_version: 1, project: args.project, snapshot_id: "fixed", total: 1, next_cursor: null, items: [{
      id: "candidate", identity_kind: "stable_item", title: "Matrix result", label: "candidate", status: "prepared", reason: "FORMAL_READY",
      binding: "current", allowed_actions: ["execute", "verify"], candidate_oid: "a".repeat(40), target: "refs/heads/formal", owner: "maintainer",
      plan: {destination: "formal", method: "squash", expected_target_oid: "b".repeat(40), tree_oid: "c".repeat(40)},
      nodes: [{id: "record", status: "recorded", reason: "FIXED_CANDIDATE"}, {id: "verify", status: "passed", reason: "FORMAL_READY"}],
      evidence: [{id: "test", binding: "current", kind: "project_test", outcome: "passed", summary: "test diagnostic line\n".repeat(120)}],
    }]};
    return {};
  }};
  const root = renderSync(<InputProvider><App client={client}/></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const wait = () => new Promise(resolve => setTimeout(resolve, 150));
  const send = async (key: string) => { stdin.write(key); await wait(); };
  const ink: any = instances.get(stdout);
  const text = () => { const screen = ink.frontFrame.screen; return Array.from({length: screen.height}, (_, y) =>
    Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || " ").join("")).join("\n"); };
  try {
    ink.setAltScreenActive(true); ink.onRender(); await wait(); await send("\r"); await wait();
    await send("/"); await send("publish"); await send("\r"); await wait(); await send("\r"); await send("\r");
    expect(text()).toContain("Saved local records");
    expect(text()).toContain("Source commit"); expect(text()).toContain("Final tree");
    expect(ink.frontFrame.screen.height).toBeLessThanOrEqual(stdout.rows);
    expect(calls.some(op => op === "publish.verify" || op === "publish.execute")).toBe(false);
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});
