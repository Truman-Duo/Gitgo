// src/main.tsx
import React from "react";
import {createInterface} from "node:readline";
import { renderSync, AlternateScreen, Box, useTerminalSize } from "@anthropic/ink";
import { NativeHostClient, type BackendClient } from "./backend/client.js";
import { MockMcpClient } from "./mock/MockMcpClient.js";
import { setBackendClient } from "./clients.js";
import { App } from "./components/App.js";
import { InputProvider, type VerificationInput } from "./input/runtime.js";
import { dirname, resolve } from "node:path";
import { existsSync } from "node:fs";
import { resolvePythonRuntime } from "./backend/pythonRuntime.js";
import {
  acceptTerminalHandoff,
  currentFrontendOrigin,
  transferTerminalOwner,
  assertInteractiveTerminal,
  launcherFromInventory,
  type TerminalInventory,
  ensureWindowsUtf8Console,
  relaunchInConfiguredTerminal,
  shouldRelaunchInConfiguredTerminal,
  windowsParentProcessName,
} from "./backend/terminalLauncher.js";
import { detectTerminals } from "./backend/tools.js";
import { registerLaunchSession } from "./backend/launchSession.js";

const acknowledgeHandoff = acceptTerminalHandoff();
process.env.GITGO_FRONTEND_ORIGIN = currentFrontendOrigin();
const temporaryLaunch = registerLaunchSession();
if (acknowledgeHandoff) assertInteractiveTerminal(process.stdin, process.stdout);
if (acknowledgeHandoff) process.stdout.write("\x1b]0;Gitgo - selected terminal\x07");

const EXECUTABLE_DIR = dirname(process.execPath);
const COMPILED = Boolean(
  process.env.GITGO_INSTALL_ROOT
  || existsSync(resolve(EXECUTABLE_DIR, "product.json"))
  || process.execPath.toLowerCase().endsWith("gitgo.exe"),
);
const GITGO_DIR = process.env.GITGO_INSTALL_ROOT
  ? resolve(process.env.GITGO_INSTALL_ROOT)
  : COMPILED ? EXECUTABLE_DIR : resolve(import.meta.dir, "../../..");
const PYTHON = resolvePythonRuntime();
const INTERNAL_HOST = (() => {
  const explicit = process.env.GITGO_HOST_EXECUTABLE || "";
  if (explicit) return explicit;
  const name = process.platform === "win32" ? "gitgo-host.exe" : "gitgo-host";
  const candidates = [
    resolve(GITGO_DIR, "internal", name),
    resolve(GITGO_DIR, "internal", "gitgo-host", name),
  ];
  return candidates.find(existsSync) || "";
})();

// ── Alt-Screen vs Main-Screen ──────────────────────────────────────────
//
// Windows (ConPTY) 默认使用 alt-screen，因为主屏幕有 resize 重复渲染问题。
//
// 根因：Ink 主屏幕渲染用 \n 换行，每帧在 scrollback 中产生大量行。
//       ConPTY 的 ResizePseudoConsole 在 resize 时会 reflow scrollback
//       历史，将旧视口内容重新注入可视区域——在应用输出之后，不受 ANSI
//       控制。详见 cli/dashboard/docs/resize-duplicate-analysis.md
//
// 三种已知解法（均无法在应用层完美解决）：
//   A. 延迟重绘 debounce — resize 后等待 ConPTY reflow 完成再重绘，时机不可靠
//   B. PSEUDOCONSOLE_RESIZE_QUIRK (0x2) — 需由 PTY host（终端模拟器）设置，
//      应用层无法控制，且非所有终端支持
//   C. Alt-Screen — alt-screen 无 scrollback，ConPTY 无历史可 reflow（Claude Code 同方案）
//
// 我们选 C：Windows 默认 alt-screen。非 Windows 平台无 ConPTY，默认主屏幕。
//
// 开关（显式，不隐藏）：
//   GITGO_ALT_SCREEN=1  → 强制 alt-screen（所有平台）
//   GITGO_ALT_SCREEN=0  → 强制主屏幕（包括 Windows，可复现 resize 重复渲染）
//   （未设置）           → Windows 默认 alt-screen，其他平台默认主屏幕
// ────────────────────────────────────────────────────────────────────────
const USE_ALT_SCREEN: boolean = (() => {
  const env = process.env.GITGO_ALT_SCREEN;
  if (env === "1") return true;
  if (env === "0") return false;
  return process.platform === "win32";
})();

function ScreenWrapper({ children }: { children: React.ReactNode }) {
  const size = useTerminalSize();
  const rows = size.rows || process.stdout.rows || 24;
  if (USE_ALT_SCREEN) {
    return <AlternateScreen mouseTracking>{children}</AlternateScreen>;
  }
  return (
    <Box flexDirection="column" height={rows} width="100%" flexShrink={0}>
      {children}
    </Box>
  );
}

const REFRESH_SEC = (() => {
  const numArg = process.argv.find((a) => /^\d+$/.test(a));
  return parseInt(numArg || "5", 10);
})();

function argumentValue(name: string): string {
  const index = process.argv.indexOf(name);
  return index >= 0 ? String(process.argv[index + 1] || "") : "";
}

function startupSmokeTask(): {
  project: string;
  message: string;
  manualDelegation: boolean;
  autoAllowOnce: boolean;
} | undefined {
  const encoded = argumentValue("--smoke-task-b64");
  if (!encoded) return undefined;
  const message = Buffer.from(encoded, "base64").toString("utf8").trim();
  if (!message) throw new Error("--smoke-task-b64 decoded to an empty task");
  return {
    project: argumentValue("--smoke-project") || "gitgo",
    message,
    manualDelegation: process.argv.includes("--smoke-manual-delegation"),
    autoAllowOnce: process.argv.includes("--smoke-auto-allow-once"),
  };
}

function verificationInput(): VerificationInput[] | undefined {
  const encoded = argumentValue("--smoke-input-b64");
  if (!encoded) return;
  const events = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
  const keys = new Set(["upArrow", "downArrow", "leftArrow", "rightArrow", "return", "escape"]);
  if (!Array.isArray(events) || events.length > 60 || events.some(event =>
    !Number.isInteger(event.afterMs) || event.afterMs < 0 || event.afterMs > 60000 ||
    typeof event.input !== "string" || event.input.length > 100 ||
    !event.key || Object.keys(event.key).some(key => !keys.has(key) || event.key[key] !== true))) {
    throw new Error("Invalid bounded verification input sequence");
  }
  return events;
}

async function main() {
  ensureWindowsUtf8Console();
  const useMock = process.argv.includes("--mock");
  const client: BackendClient = useMock
    ? (new MockMcpClient() as unknown as BackendClient)
    : new NativeHostClient(PYTHON, GITGO_DIR, INTERNAL_HOST);
  if (client instanceof NativeHostClient) {
    await client.start();
    process.stderr.write("[gitgo-dashboard] Native host mode\n");
  }
  setBackendClient(client);

  let terminals: TerminalInventory | undefined;
  let startupNotice = "";
  if (!useMock) {
    try {
      terminals = await detectTerminals(client) as TerminalInventory;
      startupNotice = (terminals.warnings || []).join(" ");
    } catch {
      startupNotice = "Terminal detection unavailable; continuing here. Retry in /config → General.";
    }
  }
  if (terminals?.configured && shouldRelaunchInConfiguredTerminal({
    compiled: COMPILED, platform: process.platform, argv: process.argv,
    parentProcessName: windowsParentProcessName(), config: launcherFromInventory(terminals),
  })) {
    process.stderr.write("[gitgo-dashboard] Opening configured terminal…\n");
    const handoff = client instanceof NativeHostClient
      ? await transferTerminalOwner(client, () => relaunchInConfiguredTerminal(launcherFromInventory(terminals!)))
      : await relaunchInConfiguredTerminal(launcherFromInventory(terminals));
    if (handoff.launched) { await client.close(); process.exit(0); }
    startupNotice = handoff.message;
  }
  if (temporaryLaunch) startupNotice = ["Temporary terminal test · isolated chat history; settings and conversations are removed when this test closes.", startupNotice].filter(Boolean).join(" ");

  let shutdownPromise: Promise<void> | null = null;
  const shutdown = (exitCode: number): Promise<void> => {
    if (shutdownPromise) return shutdownPromise;
    shutdownPromise = Promise.resolve(client.close())
      .catch(() => undefined)
      .then(() => { process.exit(exitCode); });
    return shutdownPromise;
  };
  process.once("SIGINT", () => { void shutdown(130); });
  process.once("SIGTERM", () => { void shutdown(143); });
  process.once("SIGHUP", () => { void shutdown(129); });
  process.stdin.once("end", () => { void shutdown(0); });

  let root: ReturnType<typeof renderSync>;
  const completeTerminalSetup = async () => {
    const selected = await detectTerminals(client) as TerminalInventory;
    const config = launcherFromInventory(selected);
    if (config.terminal === "current") return {completed: true, message: "Saved · continuing in the current terminal"};
    const handoff = client instanceof NativeHostClient
      ? await transferTerminalOwner(client, () => relaunchInConfiguredTerminal(config))
      : await relaunchInConfiguredTerminal(config);
    if (handoff.launched) {
      root.unmount(); root.cleanup();
      await shutdown(0);
    }
    return {completed: handoff.launched, message: handoff.message};
  };
  root = renderSync(
    <ScreenWrapper>
      <InputProvider verificationInput={verificationInput()}><App
        client={client}
        refreshSec={REFRESH_SEC}
        startupSmokeTask={startupSmokeTask()}
        startupTerminalSetup={!useMock && (!terminals || !terminals.configured)}
        startupNotice={startupNotice}
        onTerminalSetupComplete={completeTerminalSetup}
      /></InputProvider>
    </ScreenWrapper>,
    { exitOnCtrlC: false }
  );
  // Child readiness is acknowledged only after the real Host and complete
  // renderer have started, never by a hidden smoke renderer or process spawn.
  acknowledgeHandoff?.();

  await root.waitUntilExit();
  if (!shutdownPromise) await client.close();
  // Ink and terminal observers may retain timers after the renderer exits.
  // The backend is already closed, so make the CLI lifecycle deterministic.
  process.exit(0);
}

main().catch(async (err) => {
  console.error("Dashboard error:", err);
  // An Explorer-created console otherwise disappears before a startup failure
  // (including duplicate profile ownership) can be read. Never hold a pipe/CI.
  if (COMPILED && process.stdin.isTTY && process.stdout.isTTY) {
    process.stdin.setRawMode?.(false);
    const input = createInterface({input: process.stdin, output: process.stderr});
    process.stderr.write("\nPress Enter to close this window.\n");
    await new Promise<void>(resolve => {
      input.once("line", () => { input.close(); resolve(); });
      input.once("close", resolve);
    });
  }
  process.exit(1);
});
