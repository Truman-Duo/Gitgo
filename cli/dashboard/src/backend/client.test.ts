import { describe, expect, test } from "bun:test";
import {existsSync, mkdtempSync, mkdirSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join, resolve, sep} from "node:path";
import {resolvePythonRuntime} from "./pythonRuntime.js";
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
    const source = nativeHostCommand("python", "", "C:\\source checkout\\gitgo");
    expect(source.command).toBe("python");
    expect(source.args.slice(0, 2)).toEqual(["-u", "-c"]);
    expect(source.args[2]).toContain("sys.path.insert(0,sys.argv.pop(1))");
    expect(source.args[3]).toBe("C:\\source checkout\\gitgo");
  });
});


const sourcePython = resolvePythonRuntime();
const pythonAvailable = existsSync(sourcePython) || Boolean(Bun.which(sourcePython));
(pythonAvailable ? test : test.skip)("source Host imports the chosen checkout with embedded Python and spaces in its path", () => {
  const tempParent = resolve(tmpdir());
  const checkout = mkdtempSync(join(tempParent, "gitgo source host "));
  try {
    const module = join(checkout, "backend", "core");
    mkdirSync(module, {recursive: true});
    writeFileSync(join(checkout, "backend", "__init__.py"), "");
    writeFileSync(join(module, "__init__.py"), "");
    writeFileSync(join(module, "native_host.py"), "print('selected-worktree-host')");
    const command = nativeHostCommand(sourcePython, "", checkout);
    const result = Bun.spawnSync([command.command, ...command.args], {cwd: tempParent, timeout: 15000});
    expect(new TextDecoder().decode(result.stderr)).toBe("");
    expect(result.exitCode).toBe(0);
    expect(new TextDecoder().decode(result.stdout).trim()).toBe("selected-worktree-host");
  } finally {
    // Delete only the freshly created fixture under the resolved temp parent.
    if (!resolve(checkout).startsWith(tempParent + sep)) throw new Error("Unexpected fixture location");
    rmSync(checkout, {recursive: true, force: true});
  }
});
