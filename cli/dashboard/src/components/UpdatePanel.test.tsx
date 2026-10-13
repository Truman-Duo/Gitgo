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

function mount(client: BackendClient, onDismiss: () => void, onUpdateExit?: () => Promise<void>) {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin; stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 80; stdout.rows = 24;
  const stderr: any = new PassThrough();
  let text = "";
  stdout.on("data", (chunk: Buffer) => {text += chunk.toString();}); stderr.on("data", () => {});
  const root = renderSync(<InputProvider><UpdatePanel client={client} cols={80} onDismiss={onDismiss} onUpdateExit={onUpdateExit}/></InputProvider>,
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

const idle = {schema_version: 2, product_id: "gitgo-terminal", version: "1.0.0-alpha.2", status: "idle",
  enabled: false, automatic_check: false, channel: "stable", can_apply: false, can_check: true,
  can_download: false, blockers: ["UPDATE_HANDOFF_REQUIRED"], latest_version: null, candidate: null};
const candidate = {candidate_id: "a".repeat(64), version: "1.0.0-alpha.3", release_id: "release-3", bytes: 4096, channel: "stable"};
const available = {...idle, status: "available", can_download: true, latest_version: candidate.version, candidate};
const prepared = {...available, status: "downloaded", can_download: false};

describe("explicit retained rollback", () => {
  const rollback = {rollback_id: "d".repeat(64), version: "1.0.0-alpha.1"};
  const status = {...idle, schema_version: 4, can_request_apply: false, can_request_rollback: true, rollback};
  test("requires its own confirmation before clean shutdown, even offline", async () => {
    const calls: any[] = []; let exits = 0;
    const fixture = mount({ready: true, close() {}, async callTool(...args) {
      calls.push(args); return args[0] === "update.request_rollback"
        ? {schema_version: 1, status: "exit_required", action: "rollback", rollback_id: rollback.rollback_id, exit_code: 75}
        : {...status, status: "unconfigured", can_check: false};
    }}, () => {}, async () => {exits++;});
    try {
      await wait(); fixture.stdin.write("\r"); await wait(); expect(calls).toHaveLength(1);
      fixture.stdin.write("b"); await wait(); expect(calls).toHaveLength(1);
      expect(fixture.text()).toContain("restore Gitgo 1.0.0-alpha.1");
      fixture.stdin.write("n"); await wait(); expect(exits).toBe(0);
      fixture.stdin.write("b"); await wait(); fixture.stdin.write("\r"); await wait();
      expect(calls[1][0]).toBe("update.request_rollback");
      expect(calls[1][1].rollback_id).toBe(rollback.rollback_id);
      expect(exits).toBe(1);
    } finally {fixture.close();}
  });
  test("cancelled rollback acknowledgement cannot close the session", async () => {
    let finish!: (value: any) => void; let exits = 0;
    const fixture = mount({ready: true, close() {}, callTool(operation) {
      return operation === "update.request_rollback" ? new Promise(resolve => {finish = resolve;})
        : Promise.resolve(operation === "update.cancel" ? {cancelled: true} : status);
    }}, () => {}, async () => {exits++;});
    try {
      await wait(); fixture.stdin.write("b"); await wait(); fixture.stdin.write("\r"); await wait();
      fixture.stdin.write("x"); await wait();
      finish({schema_version: 1, status: "exit_required", action: "rollback", rollback_id: rollback.rollback_id, exit_code: 75});
      await wait(); expect(exits).toBe(0);
    } finally {fixture.close();}
  });
});

describe("confirmed updater handoff", () => {
  test("separate confirmation and exact response are required before shutdown", async () => {
    const calls: string[] = []; let exits = 0;
    const fixture = mount({ready: true, close() {}, async callTool(operation) {
      calls.push(operation);
      return operation === "update.request_apply"
        ? {schema_version: 1, status: "exit_required", channel: "stable", candidate_id: candidate.candidate_id, exit_code: 75}
        : {...prepared, schema_version: 3, can_request_apply: true};
    }}, () => {}, async () => {exits++;});
    try {
      await wait(); fixture.stdin.write("\r"); await wait(); expect(calls).toHaveLength(1);
      fixture.stdin.write("a"); await wait(); expect(calls).toHaveLength(1);
      expect(fixture.text()).toContain("Close this Gitgo session");
      fixture.stdin.write("n"); await wait(); expect(exits).toBe(0);
      fixture.stdin.write("a"); await wait(); fixture.stdin.write("\r"); await wait();
      fixture.stdin.write("\r"); await wait();
      expect(calls.filter(call => call === "update.request_apply")).toHaveLength(1);
      expect(exits).toBe(1);
    } finally {fixture.close();}
  });
  test("cancelled late acknowledgement never triggers shutdown", async () => {
    let acknowledge!: (value: any) => void; let exits = 0;
    const fixture = mount({ready: true, close() {}, callTool(operation) {
      if (operation === "update.request_apply") return new Promise(resolve => {acknowledge = resolve;});
      return Promise.resolve(operation === "update.cancel" ? {cancelled: true} : {...prepared, schema_version: 3, can_request_apply: true});
    }}, () => {}, async () => {exits++;});
    try {
      await wait(); fixture.stdin.write("a"); await wait(); fixture.stdin.write("\r"); await wait();
      fixture.stdin.write("x"); await wait();
      acknowledge({schema_version: 1, status: "exit_required", channel: "stable", candidate_id: candidate.candidate_id, exit_code: 75});
      await wait(); expect(exits).toBe(0);
    } finally {fixture.close();}
  });
  test("an unsupported launcher cannot request application", async () => {
    const calls: string[] = [];
    const fixture = mount({ready: true, close() {}, async callTool(operation) {
      calls.push(operation); return {...prepared, schema_version: 3, can_request_apply: true};
    }}, () => {});
    try {
      await wait(); fixture.stdin.write("a\r"); await wait(); expect(calls).toHaveLength(1);
      expect(fixture.text()).toContain("Restart/update is unavailable");
    } finally {fixture.close();}
  });
});

describe("manual update preparation UI", () => {
  test("opens offline, explicitly checks, and requires separate download confirmation", async () => {
    const calls: any[] = [];
    let result = idle;
    const fixture = mount({ready: true, close() {}, async callTool(...args) {
      calls.push(args);
      if (args[0] === "update.check") result = available as any;
      if (args[0] === "update.download") result = prepared as any;
      return result;
    }}, () => {});
    try {
      await wait(); expect(calls.map(call => call[0])).toEqual(["update.status"]);
      fixture.stdin.write("\r"); await wait(); expect(calls).toHaveLength(1);
      fixture.stdin.write("k"); await wait();
      expect(calls[1][0]).toBe("update.check");
      expect(fixture.text()).toContain("1.0.0-alpha.3");
      fixture.stdin.write("d"); await wait(); expect(calls).toHaveLength(2);
      expect(fixture.text()).toContain("Download this exact signed package?");
      fixture.stdin.write("n"); await wait(); expect(calls).toHaveLength(2);
      fixture.stdin.write("d"); await wait(); fixture.stdin.write("\r"); await wait();
      expect(calls[2][0]).toBe("update.download");
      expect(calls[2][1].candidate_id).toBe(candidate.candidate_id);
      expect(fixture.text()).toContain("Downloaded and verified");
      expect(calls.some(call => call[0] === "update.apply")).toBe(false);
    } finally {fixture.close();}
  });
  test("changing channels is only a local refresh; paste and Enter do not check", async () => {
    const calls: any[] = [];
    const fixture = mount({ready: true, close() {}, async callTool(...args) {
      calls.push(args); return {...idle, channel: args[1]?.channel || "stable"};
    }}, () => {});
    try {
      await wait(); fixture.stdin.write("c"); await wait();
      expect(calls).toEqual([["update.status"], ["update.status", {channel: "preview"}]]);
      fixture.stdin.write("\x1b[200~k\x1b[201~"); await wait();
      fixture.stdin.write("\r"); await wait();
      expect(calls).toHaveLength(2);
    } finally {fixture.close();}
  });
  test("missing publisher configuration never sends check/download requests", async () => {
    const calls: any[] = [];
    const fixture = mount({ready: true, close() {}, async callTool(...args) {
      calls.push(args); return {...idle, status: "unconfigured", can_check: false};
    }}, () => {});
    try {
      await wait(); fixture.stdin.write("kd\r"); await wait();
      expect(calls).toHaveLength(1);
      expect(fixture.text()).toContain("not configured");
    } finally {fixture.close();}
  });
  test("in-flight cancellation uses the same job ID, with no duplicate download", async () => {
    const calls: any[] = []; let reject!: (error: Error) => void;
    const fixture = mount({ready: true, close() {}, callTool(...args) {
      calls.push(args);
      if (args[0] === "update.download") return new Promise((_yes, no) => {reject = no;});
      if (args[0] === "update.cancel") {reject(new Error("private-proxy-password")); return Promise.resolve({cancelled: true});}
      return Promise.resolve(available);
    }}, () => {});
    try {
      await wait(); fixture.stdin.write("d"); await wait(); fixture.stdin.write("\r"); await wait();
      fixture.stdin.write("d\r"); await wait();
      expect(calls.filter(call => call[0] === "update.download")).toHaveLength(1);
      fixture.stdin.write("x"); await wait();
      expect(calls[2][0]).toBe("update.cancel");
      expect(calls[2][1].operation_id).toBe(calls[1][1].operation_id);
      expect(fixture.text()).toContain("No software was installed");
      expect(fixture.text()).not.toContain("private-proxy-password");
    } finally {fixture.close();}
  });
  test("closing a pending check cancels it and ignores late completion", async () => {
    const calls: any[] = []; let complete!: (data: unknown) => void; let dismissed = 0;
    const fixture = mount({ready: true, close() {}, callTool(...args) {
      calls.push(args);
      if (args[0] === "update.check") return new Promise(yes => {complete = yes;});
      return Promise.resolve(args[0] === "update.cancel" ? {cancelled: true} : idle);
    }}, () => {dismissed++;});
    try {
      await wait(); fixture.stdin.write("k"); await wait(); fixture.stdin.write("\x1b");
      await new Promise(resolve => setTimeout(resolve, 100));
      expect(dismissed).toBe(1);
      expect(calls.find(call => call[0] === "update.cancel")?.[1].operation_id).toBe(calls[1][1].operation_id);
    } finally {fixture.close();}
    complete(available); await wait();
  });
});
