import { describe, expect, test } from "bun:test";
import {
  globalConfigPath,
  isShellLauncherProcess,
  shouldRelaunchInConfiguredTerminal,
  terminalChildCommand,
  relaunchInConfiguredTerminal,
  acceptTerminalHandoff,
  assertInteractiveTerminal,
  transferTerminalOwner,
  currentFrontendOrigin,
} from "./terminalLauncher.js";
import { mkdtempSync, writeFileSync, readFileSync, unlinkSync, rmdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID, createHash } from "node:crypto";

describe("installed terminal launcher", () => {
  test("noninteractive child cannot acknowledge formal terminal readiness", () => {
    expect(() => assertInteractiveTerminal({isTTY: false}, {isTTY: true})).toThrow("TERMINAL_NOT_INTERACTIVE");
    expect(() => assertInteractiveTerminal({isTTY: true}, {isTTY: false})).toThrow("TERMINAL_NOT_INTERACTIVE");
    expect(() => assertInteractiveTerminal({isTTY: true}, {isTTY: true})).not.toThrow();
  });
  test("a new packaged invocation from a shell uses the saved preference", () => {
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe"],
      parentProcessName: "powershell.exe",
      config: {terminal: "auto", command: "", args: []},
    })).toBeTrue();
  });

  test("handoff releases the original backend first and restores it on failure", async () => {
    const events: string[] = [];
    const client = {close: async () => {events.push("close");}, start: async () => {events.push("restart");}};
    const result = await transferTerminalOwner(client, async () => {
      events.push("launch"); return {launched: false, message: "failed"};
    });
    expect(events).toEqual(["close", "launch", "restart"]);
    expect(result.launched).toBeFalse();
    events.length = 0;
    await transferTerminalOwner(client, async () => {events.push("launch"); return {launched: true, message: "ready"};});
    expect(events).toEqual(["close", "launch"]);
  });

  test("actual origin follows the current host environment, not next-launch preference", () => {
    expect(JSON.parse(currentFrontendOrigin({WT_SESSION: "session"}, "win32")).terminal).toBe("windows_terminal");
    const origin = JSON.stringify({version: 1, terminal: "git_bash", platform: "win32", source: "terminal_handoff"});
    expect(currentFrontendOrigin({GITGO_FRONTEND_ORIGIN: origin, WT_SESSION: "stale"}, "win32")).toBe(origin);
    expect(shouldRelaunchInConfiguredTerminal({compiled: true, platform: "win32", argv: ["gitgo.exe", "--attached"],
      parentProcessName: "cmd", config: {terminal: "git_bash", command: "any", args: []}})).toBeFalse();
  });

  test("relaunches an Explorer invocation exactly once", () => {
    const config = {terminal: "auto" as const, command: "", args: []};
    expect(isShellLauncherProcess("explorer.exe")).toBeTrue();
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe"],
      parentProcessName: "Explorer", config,
    })).toBeTrue();
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe", "--attached"],
      parentProcessName: "Explorer", config,
    })).toBeFalse();
  });

  test("builds Windows Terminal and configurable terminal child commands", () => {
    expect(terminalChildCommand(
      {terminal: "windows_terminal", command: "", args: []},
      "C:\\Gitgo\\gitgo.exe", ["5"], "C:\\WindowsApps\\wt.exe",
    )).toEqual({
      command: "C:\\WindowsApps\\wt.exe",
      args: ["new-tab", "--", "C:\\Gitgo\\gitgo.exe", "--attached", "5"],
    });
    expect(terminalChildCommand(
      {terminal: "custom", command: "wezterm.exe", args: ["start", "--"]},
      "C:\\Gitgo\\gitgo.exe", [],
    )).toEqual({
      command: "wezterm.exe",
      args: ["start", "--", "C:\\Gitgo\\gitgo.exe", "--attached"],
    });
  });

  test("uses the global config location or the explicit isolation path", () => {
    expect(globalConfigPath({}, "C:\\Users\\Ada")).toBe("C:\\Users\\Ada\\.gitgo\\config.json");
    expect(globalConfigPath({GITGO_CONFIG_PATH: "C:\\Temp\\isolated.json"}, "ignored"))
      .toBe("C:\\Temp\\isolated.json");
  });

  test("source entry precedes flags and key automation is not repeated in the child", () => {
    const child = terminalChildCommand({terminal: "git_bash", command: "git-bash.exe", args: ["bash-args"]},
      "bun.exe", ["C:\\source folder\\main.tsx", "--attached", "--smoke-input-b64", "keys", "--smoke-project", "existing project"]);
    expect(child?.args).toEqual(["--dir", process.cwd(), "bash-args", "bun.exe", "C:\\source folder\\main.tsx", "--attached", "--smoke-project", "existing project"]);
  });

  test("a new unverified DLL blocks launch even when checked images have not changed", async () => {
    const dir = mkdtempSync(join(tmpdir(), "gitgo-dependency-test-"));
    const path = join(dir, "never-run.exe");
    const dll = join(dir, "injected.dll");
    writeFileSync(path, "unchanged image");
    writeFileSync(dll, "unknown library");
    try {
      const result = await relaunchInConfiguredTerminal({terminal: "git_bash", command: path, args: [],
        identity: {verified: true, files: [{path, sha256: createHash("sha256").update(readFileSync(path)).digest("hex")}],
          directories: [{path: dir, files: []}]}});
      expect(result.launched).toBeFalse();
      expect(result.message).toContain("TERMINAL_IDENTITY_CHANGED");
    } finally {unlinkSync(dll); unlinkSync(path); rmdirSync(dir);}
  });

  test("changed verified binary is rejected before any child is launched", async () => {
    const dir = mkdtempSync(join(tmpdir(), "gitgo-identity-test-"));
    const path = join(dir, "never-run.exe");
    writeFileSync(path, "changed bytes");
    try {
      const result = await relaunchInConfiguredTerminal({terminal: "git_bash", command: path, args: [],
        identity: {verified: true, files: [{path, sha256: "old-fingerprint"}]} });
      expect(result.launched).toBeFalse();
      expect(result.message).toContain("TERMINAL_IDENTITY_CHANGED");
    } finally { unlinkSync(path); rmdirSync(dir); }
  });

  test("expired handoff is rejected and ready acknowledgement is explicit", () => {
    const directory = mkdtempSync(join(tmpdir(), "gitgo-terminal-"));
    const path = join(directory, "handoff.json");
    const token = randomUUID();
    const argv = ["--launcher-handoff", path, "--launcher-handoff-token", token];
    const previous = {...process.env};
    try {
      writeFileSync(path, JSON.stringify({token, status: "pending", expiresAt: Date.now() - 1, environment: {}}));
      expect(() => acceptTerminalHandoff(argv)).toThrow("expired");
      writeFileSync(path, JSON.stringify({token, status: "pending", expiresAt: Date.now() + 5000, environment: {
        GITGO_CONFIG_PATH: "isolated.json", GITGO_LAUNCH_SESSION: "session.json",
        GITGO_LAUNCH_SESSION_TOKEN: token, ATTACKER_SETTING: "ignored"}}));
      const acknowledge = acceptTerminalHandoff(argv);
      expect(process.env.GITGO_CONFIG_PATH).toBe("isolated.json");
      expect(process.env.GITGO_LAUNCH_SESSION).toBe("session.json");
      expect(process.env.GITGO_LAUNCH_SESSION_TOKEN).toBe(token);
      expect(process.env.ATTACKER_SETTING).toBeUndefined();
      expect(JSON.parse(readFileSync(path, "utf8")).status).toBe("pending");
      acknowledge!();
      expect(JSON.parse(readFileSync(path, "utf8")).status).toBe("ready");
      writeFileSync(path, JSON.stringify({token, status: "pending", expiresAt: Date.now() + 5000, environment: {}}));
      acceptTerminalHandoff(argv);
      expect(process.env.GITGO_LAUNCH_SESSION).toBeUndefined();
      expect(process.env.GITGO_LAUNCH_SESSION_TOKEN).toBeUndefined();
    } finally {
      for (const key of Object.keys(process.env)) if (!(key in previous)) delete process.env[key];
      Object.assign(process.env, previous);
      unlinkSync(path); rmdirSync(directory);
    }
  });

  test("a launcher exiting successfully without a ready child retains the original terminal", async () => {
    const result = await relaunchInConfiguredTerminal({terminal: "custom", command: process.execPath,
      args: ["-e", "process.exit(0)", "--"]}, process.execPath, [], {timeoutMs: 200});
    expect(result.launched).toBeFalse();
    expect(result.message).toContain("did not become ready");
  });

  test("a failing launcher retains the original terminal with an error", async () => {
    const result = await relaunchInConfiguredTerminal({terminal: "custom", command: process.execPath,
      args: ["-e", "process.exit(7)", "--"]}, process.execPath, [], {timeoutMs: 500});
    expect(result.launched).toBeFalse();
    expect(result.message).toContain("exited (7)");
  });

  test("child acknowledgement rather than spawn alone completes the handoff", async () => {
    const program = `const fs = require('node:fs'); const i = process.argv.indexOf('--launcher-handoff');
      const path = process.argv[i+1]; const ticket = JSON.parse(fs.readFileSync(path,'utf8'));
      fs.writeFileSync(path, JSON.stringify({...ticket,status:'ready'}));`;
    const result = await relaunchInConfiguredTerminal({terminal: "custom", command: process.execPath,
      args: ["-e", program, "--"]}, process.execPath, [], {timeoutMs: 2000});
    expect(result.launched).toBeTrue();
  });
});
