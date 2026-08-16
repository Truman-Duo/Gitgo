// src/clients.ts — shared client references for hybrid (native + MCP) mode.
// In --native mode, main.tsx sets both; hooks read the one they need.

import type { McpClient } from "./mcp/client.js";
import type { DaemonClient } from "./daemon/client.js";

let _mcp: McpClient | null = null;
let _daemon: DaemonClient | null = null;

export function setMcpClient(c: McpClient) { _mcp = c; }
export function setDaemonClient(c: DaemonClient) { _daemon = c; }

export function getMcpClient(): McpClient | null { return _mcp; }
export function getDaemonClient(): DaemonClient | null { return _daemon; }
