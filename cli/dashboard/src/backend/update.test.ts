import {describe, expect, test} from "bun:test";
import {productVersion, updateStatus, parseUpdateStatus, checkUpdate, downloadUpdate, cancelUpdate, requestApplyUpdate, requestRollbackUpdate} from "./update.js";
import type {BackendClient} from "./client.js";

const sample = {schema_version: 1, product_id: "gitgo-terminal", version: "1.0.0-alpha.2",
  status: "disabled", enabled: false, automatic_check: false, channel: "stable", can_apply: false,
  blockers: ["MANAGED_INSTALLATION_REQUIRED"]};
const client = (data: unknown): BackendClient => ({ready: true, close() {}, async callTool() {return data;}});

describe("confirmed handoff protocol", () => {
  const ack = {schema_version: 1, status: "exit_required", exit_code: 75,
    channel: "preview", candidate_id: "a".repeat(64)};
  test("requires an acknowledgement for the exact confirmed selection", async () => {
    await requestApplyUpdate(client(ack), "preview", ack.candidate_id, "b".repeat(32));
  });
  for (const patch of [{schema_version: 2}, {status: "installed"}, {exit_code: 0},
    {exit_code: "75"}, {channel: "stable"}, {candidate_id: "c".repeat(64)}]) {
    test(`rejects mismatched shutdown acknowledgement ${JSON.stringify(patch)}`, async () => {
      await expect(requestApplyUpdate(client({...ack, ...patch}), "preview", ack.candidate_id,
        "b".repeat(32))).rejects.toThrow("Update handoff mismatch");
    });
  }
});

describe("update status protocol", () => {
  test("uses local application operations without project arguments", async () => {
    const calls: any[] = [];
    const c: BackendClient = {ready: true, close() {}, async callTool(...args) {calls.push(args); return sample;}};
    expect((await productVersion(c)).version).toBe(sample.version);
    expect((await updateStatus(c)).enabled).toBe(false);
    expect(calls).toEqual([["product.version"], ["update.status"]]);
  });
  for (const patch of [
    {version: "\x1b[31msecret"}, {version: "x".repeat(129)}, {version: "01.0.0"},
    {version: "1.0.0\n"}, {version: "1.0.0-."}, {version: "1.0.0-01"},
    {product_id: "other"}, {schema_version: 2}, {enabled: true}, {can_apply: true},
    {status: "available"}, {automatic_check: true}, {channel: "preview"},
    {blockers: "not-an-array"}, {blockers: ["\x1b[31m"]}, {blockers: ["INVALID\n"]},
  ]) test(`rejects unsupported or unsafe status ${JSON.stringify(patch)}`, async () => {
    await expect(updateStatus(client({...sample, ...patch}))).rejects.toThrow();
  });
  test("missing old-host operation never guesses a current version", async () => {
    await expect(productVersion(client({}))).rejects.toThrow("Product version unavailable");
  });
});

const available = {schema_version: 2, product_id: "gitgo-terminal", version: "1.0.0-alpha.2",
  status: "available", enabled: false, automatic_check: false, channel: "preview", can_apply: false,
  can_check: true, can_download: true, blockers: ["UPDATE_HANDOFF_REQUIRED"], latest_version: "1.0.0-alpha.3",
  candidate: {candidate_id: "a".repeat(64), version: "1.0.0-alpha.3", release_id: "release-3", bytes: 4096, channel: "preview"}};

describe("manual check/download protocol", () => {
  test("allows separate manual capabilities but never enables apply", () => {
    expect(parseUpdateStatus(available).can_apply).toBe(false);
    expect(parseUpdateStatus({...available, status: "downloaded", can_download: false}).status).toBe("downloaded");
  });
  for (const patch of [
    {can_apply: true}, {enabled: true}, {automatic_check: true}, {status: "installed"},
    {can_download: false}, {can_check: false}, {candidate: null}, {latest_version: null},
    {candidate: {...available.candidate, candidate_id: "a".repeat(64) + "\n"}},
    {candidate: {...available.candidate, release_id: "unsafe\x1b[31m"}},
    {candidate: {...available.candidate, bytes: 2 * 1024 ** 3 + 1}},
    {candidate: {...available.candidate, channel: "stable"}},
  ]) test(`rejects capability/payload mismatch ${JSON.stringify(patch)}`, () => {
    expect(() => parseUpdateStatus({...available, ...patch})).toThrow();
  });
  test("uses only channel/selection/job IDs, never UI supplied trust or local paths", async () => {
    const calls: any[] = [];
    const c: BackendClient = {ready: true, close() {}, async callTool(...args) {
      calls.push(args); return args[0] === "update.cancel" ? {cancelled: true} : args[0] === "update.download"
        ? {...available, status: "downloaded", can_download: false} : available;
    }};
    await checkUpdate(c, "preview", "b".repeat(32));
    await downloadUpdate(c, "preview", "a".repeat(64), "c".repeat(32));
    await cancelUpdate(c, "c".repeat(32));
    expect(calls).toEqual([
      ["update.check", {channel: "preview", operation_id: "b".repeat(32)}, 180],
      ["update.download", {channel: "preview", candidate_id: "a".repeat(64), operation_id: "c".repeat(32)}, 1200],
      ["update.cancel", {operation_id: "c".repeat(32)}, 10],
    ]);
  });
});

describe("retained rollback protocol", () => {
  const rollback = {rollback_id: "d".repeat(64), version: "1.0.0-alpha.1"};
  const status = {...available, schema_version: 4, can_request_apply: false, can_request_rollback: true, rollback};
  test("rollback remains available offline without publisher trust", () => {
    const parsed = parseUpdateStatus({...status, status: "unconfigured", can_check: false, can_download: false,
      latest_version: null, candidate: null});
    expect(parsed.schema_version).toBe(4);
    expect(parsed.schema_version !== 1 && parsed.can_request_rollback).toBe(true);
  });
  for (const patch of [{rollback: null}, {can_request_rollback: "true"},
    {rollback: {...rollback, rollback_id: "../wrong"}}, {rollback: {...rollback, version: "bad\x1b"}}]) {
    test(`rejects invalid rollback selection ${JSON.stringify(patch)}`, () => {
      expect(() => parseUpdateStatus({...status, ...patch})).toThrow();
    });
  }
  test("only sends exact reviewed ID, and rejects an unrelated acknowledgement", async () => {
    const calls: any[] = [];
    const ack = {schema_version: 1, status: "exit_required", action: "rollback", exit_code: 75, rollback_id: rollback.rollback_id};
    await requestRollbackUpdate({ready: true, close() {}, async callTool(...args) {calls.push(args); return ack;}},
      rollback.rollback_id, "e".repeat(32));
    expect(calls).toEqual([["update.request_rollback", {rollback_id: rollback.rollback_id, operation_id: "e".repeat(32)}, 180]]);
    for (const patch of [{action: "apply"}, {exit_code: 0}, {rollback_id: "f".repeat(64)}]) {
      await expect(requestRollbackUpdate(client({...ack, ...patch}), rollback.rollback_id, "e".repeat(32))).rejects.toThrow();
    }
  });
});
