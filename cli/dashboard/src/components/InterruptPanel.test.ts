import { expect, test } from "bun:test";
import { requestInterruption } from "./InterruptPanel.js";

test("interrupt falls back from stale request correlation to the durable process", async () => {
  const calls: Array<{ operation: string; args: Record<string, unknown> }> = [];
  const client = {
    callTool: async (operation: string, args: Record<string, unknown>) => {
      calls.push({ operation, args });
      if (operation === "host.cancel") return { cancelled: false, reason: "request_not_active" };
      return { requested: true, process_ids: ["process-1"], status: "cancelling" };
    },
  } as any;

  const result = await requestInterruption(
    client, "project-1", "process-1", "request-1",
  );

  expect(result.requested).toBe(true);
  expect(calls).toEqual([
    { operation: "host.cancel", args: { request_id: "request-1" } },
    { operation: "runtime.stop", args: { project: "project-1", process_id: "process-1" } },
  ]);
});
