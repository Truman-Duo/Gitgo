import { describe, expect, test } from "bun:test";
import {
  buildNativeTaskCall, nativeHostCommand, NativeHostClient, stringifyProtocolJson,
} from "./client.js";

describe("native task routing", () => {
  test("correlated Host ACK adjusts the transport deadline and UTF8 chunks stay intact", async () => {
    const client: any = new NativeHostClient("unused", "unused");
    let sent: any;
    const events: any[] = [];
    client._ready = true;
    client.proc = {exitCode: null, stdin: {write: (text: string) => {sent = JSON.parse(text);}}};
    client.subscribeEvents((event: any) => events.push(event));
    const pending = client.callTool("runtime.chat", {project: "demo"}, 0.01);
    client.onData(JSON.stringify({protocol_version: 1, type: "event", project: "demo", request_id: sent.request_id,
      payload: {event: "runtime_ack", wait_timeout_seconds: 1}}) + "\n");
    await new Promise(resolve => setTimeout(resolve, 30));
    const bytes = Buffer.from(JSON.stringify({protocol_version: 1, type: "response", request_id: sent.request_id,
      ok: true, result: {response: "中文输出"}}) + "\n");
    for (const byte of bytes) client.onData(client.stdoutDecoder.write(Buffer.from([byte])));
    expect(await pending).toEqual({response: "中文输出"});
    expect(client.pending.size).toBe(0);
    expect(events).toEqual([expect.objectContaining({event: "runtime_ack", project: "demo"})]);
  });
  test("ordinary chat is never reclassified from project state", () => {
    const call = buildNativeTaskCall({ project: "demo", message: "new task" });
    expect(call.operation).toBe("runtime.chat");
    expect(call.arguments).toEqual({
      project: "demo", message: "new task",
      message_utf8_sha256: "90fa7fff4709814127b689289a22f0491d4c3577636ab42caa91b8a52dc886d1",
    });
  });

  test("rejects malformed UTF-16 before it reaches the Host", () => {
    expect(() => buildNativeTaskCall({project: "demo", message: "bad\udcaf"}))
      .toThrow("INPUT_ENCODING_CORRUPTED");
  });

  test("writes the process protocol as ASCII while preserving Unicode scalars", () => {
    const value = {message: "中文与罕见字符𠮷", nested: ["天气"]};
    const wire = stringifyProtocolJson(value);
    expect(Buffer.from(wire, "utf8").every(byte => byte < 0x80)).toBe(true);
    expect(wire).toContain("\\ud842\\udfb7");
    expect(JSON.parse(wire)).toEqual(value);
  });

  test("decision submission requires the full explicit identity", () => {
    const call = buildNativeTaskCall({
      project: "demo",
      message: "Option 1",
      decision: { task_id: "task-1", process_id: "process-1", decision_id: "decision-1" },
    });
    expect(call.operation).toBe("runtime.decision");
    expect(call.arguments).toMatchObject({
      task_id: "task-1", process_id: "process-1", decision_id: "decision-1",
    });
  });

  test("installed product starts its bundled Host without exposing Python", () => {
    expect(nativeHostCommand("python", "C:\\Gitgo\\internal\\gitgo-host.exe")).toEqual({
      command: "C:\\Gitgo\\internal\\gitgo-host.exe", args: [],
    });
    expect(nativeHostCommand("python", "")).toEqual({
      command: "python", args: ["-u", "-m", "backend.core.native_host"],
    });
  });
});
