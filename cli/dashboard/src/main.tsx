// src/main.tsx
import React from "react";
import { renderSync, AlternateScreen, Box, useTerminalSize } from "@anthropic/ink";
import { McpClient } from "./mcp/client.js";
import { DaemonClient } from "./daemon/client.js";
import { MockMcpClient } from "./mock/MockMcpClient.js";
import { setMcpClient, setDaemonClient } from "./clients.js";
import { App } from "./components/App.js";
import { resolve } from "node:path";

const GITGO_DIR = resolve(import.meta.dir, "../../..");
const PYTHON =
  process.platform === "win32"
    ? "C:/Users/Duo/AppData/Local/Programs/Python/Python312/python.exe"
    : "python3";
const MCP_SERVER = resolve(GITGO_DIR, "mcp_server.py");

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
    return <AlternateScreen mouseTracking={false}>{children}</AlternateScreen>;
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

async function main() {
  const useMock = process.argv.includes("--mock");
  const useNative = process.argv.includes("--native");
  const nativeProject = useNative
    ? (process.argv[process.argv.indexOf("--native") + 1] || "gitgo")
    : "gitgo";

  if (useNative) {
    // Native daemon (loop/chat) + MCP (data queries)
    const daemon = new DaemonClient(nativeProject, PYTHON);
    await daemon.start();
    setDaemonClient(daemon);

    const mcp = new McpClient(PYTHON, MCP_SERVER);
    await new Promise((r) => setTimeout(r, 500));
    setMcpClient(mcp);

    process.stderr.write("[gitgo-dashboard] Native+ MCP hybrid mode\n");

    const { waitUntilExit } = renderSync(
      <ScreenWrapper>
        <App client={mcp} refreshSec={REFRESH_SEC} />
      </ScreenWrapper>,
      { exitOnCtrlC: false }
    );

    await waitUntilExit();
    mcp.close();
    daemon.close();
  } else {
    const client: McpClient = useMock
      ? (new MockMcpClient() as unknown as McpClient)
      : new McpClient(PYTHON, MCP_SERVER);

    // Wait for MCP handshake to settle (up to 5 seconds) — real client only
    if (!useMock) {
      await new Promise((r) => setTimeout(r, 500));
    }

    const { waitUntilExit } = renderSync(
      <ScreenWrapper>
        <App client={client} refreshSec={REFRESH_SEC} />
      </ScreenWrapper>,
      { exitOnCtrlC: false }
    );

    await waitUntilExit();
    client.close();
  }
}

main().catch((err) => {
  console.error("Dashboard error:", err);
  process.exit(1);
});
