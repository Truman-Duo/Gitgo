import {describe, expect, test} from "bun:test";
import {productVersion, updateStatus} from "./update.js";
import type {BackendClient} from "./client.js";

const sample = {schema_version: 1, product_id: "gitgo-terminal", version: "1.0.0-alpha.2",
  status: "disabled", enabled: false, automatic_check: false, channel: "stable", can_apply: false,
  blockers: ["MANAGED_INSTALLATION_REQUIRED"]};
const client = (data: unknown): BackendClient => ({ready: true, close() {}, async callTool() {return data;}});

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
