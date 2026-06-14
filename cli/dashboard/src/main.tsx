// src/main.tsx
import React from "react";
import { renderSync } from "@anthropic/ink";
import { McpClient } from "./mcp/client.js";
import { App } from "./components/App.js";
import { resolve } from "node:path";

const GITGO_DIR = resolve(import.meta.dir, "../../..");
const PYTHON =
  process.platform === "win32"
    ? "C:/Users/Duo/AppData/Local/Programs/Python/Python312/python.exe"
    : "python3";
const MCP_SERVER = resolve(GITGO_DIR, "mcp_server.py");

const REFRESH_SEC = parseInt(process.argv[2] || "5", 10);

async function main() {
  process.stderr.write("[gitgo-dashboard] Starting MCP client...\n");
  const client = new McpClient(PYTHON, MCP_SERVER);

  // Wait for MCP handshake to settle (up to 5 seconds)
  await new Promise((r) => setTimeout(r, 500));
  process.stderr.write("[gitgo-dashboard] Rendering UI...\n");

  const { waitUntilExit } = renderSync(
    <App client={client} refreshSec={REFRESH_SEC} />,
    { exitOnCtrlC: true }
  );

  await waitUntilExit();
  client.close();
  process.stderr.write("[gitgo-dashboard] Done.\n");
}

main().catch((err) => {
  console.error("Dashboard error:", err);
  process.exit(1);
});
