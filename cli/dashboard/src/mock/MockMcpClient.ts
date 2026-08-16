// src/mock/MockMcpClient.ts — Mock MCP client for --mock mode
// Returns canned data from mockData.ts, simulating a real MCP server.

import { MCP_MOCK_MAP } from "./mockData.js";

export class MockMcpClient {
  private _ready = true;

  get ready(): boolean {
    return this._ready;
  }

  async callTool(
    toolName: string,
    args: Record<string, any> = {},
  ): Promise<any> {
    // Simulate network latency (50-150ms)
    const delay = 50 + Math.random() * 100;
    await new Promise((r) => setTimeout(r, delay));

    const handler = MCP_MOCK_MAP[toolName];
    if (handler) {
      return handler(args);
    }
    return { error: `Unknown tool: ${toolName}` };
  }

  close() {
    this._ready = false;
  }
}
