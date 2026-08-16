// src/main.tsx
import React from "react";
import { renderSync, AlternateScreen } from "@anthropic/ink";
import { McpClient } from "./mcp/client.js";
import { DaemonClient } from "./daemon/client.js";
import { setMcpClient, setDaemonClient } from "./clients.js";
import { App } from "./components/App.js";
import { resolve } from "node:path";

const GITGO_DIR = resolve(import.meta.dir, "../../..");
const PYTHON =
  process.platform === "win32"
    ? "C:/Users/Duo/AppData/Local/Programs/Python/Python312/python.exe"
    : "python3";
const MCP_SERVER = resolve(GITGO_DIR, "mcp_server.py");

const REFRESH_SEC = (() => {
  const numArg = process.argv.find((a) => /^\d+$/.test(a));
  return parseInt(numArg || "5", 10);
})();

async function main() {
  const useNative = process.argv.includes("--native");
  const project = useNative
    ? (process.argv[process.argv.indexOf("--native") + 1] || "gitgo")
    : "gitgo";

  if (useNative) {
    // Path B: native daemon (loop) + MCP (data queries)
    const daemon = new DaemonClient(project, PYTHON);
    await daemon.start();
    setDaemonClient(daemon);

    const mcp = new McpClient(PYTHON, MCP_SERVER);
    await new Promise((r) => setTimeout(r, 500));
    setMcpClient(mcp);

    process.stderr.write("[gitgo-dashboard] Native+ MCP hybrid mode\n");

    const { waitUntilExit } = renderSync(
      <AlternateScreen mouseTracking={false}>
        <App client={mcp} refreshSec={REFRESH_SEC} />
      </AlternateScreen>,
      { exitOnCtrlC: false }
    );

    await waitUntilExit();
    mcp.close();
    daemon.close();
  } else {
    // Path A: MCP only (default)
    process.stderr.write("[gitgo-dashboard] MCP mode\n");
    const client = new McpClient(PYTHON, MCP_SERVER);
    await new Promise((r) => setTimeout(r, 500));

    const { waitUntilExit } = renderSync(
      <AlternateScreen mouseTracking={false}>
        <App client={client} refreshSec={REFRESH_SEC} />
      </AlternateScreen>,
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
