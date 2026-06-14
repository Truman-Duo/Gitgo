// src/mcp/client.ts — MCP stdio client for gitgo mcp_server.py
import { spawn, type ChildProcess } from "node:child_process";
import { join } from "node:path";

type JsonRpcRequest = {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params: any;
};

type JsonRpcResponse = {
  jsonrpc: "2.0";
  id: number;
  result?: any;
  error?: { code: number; message: string };
};

export class McpClient {
  private proc: ChildProcess;
  private requestId = 0;
  private pending = new Map<
    number,
    { resolve: Function; reject: Function }
  >();
  private buffer = "";
  private _ready = false;
  private _readyPromise: Promise<void>;
  private _exitCode: number | null = null;

  constructor(pythonPath: string, mcpServerPath: string) {
    this.proc = spawn(pythonPath, [mcpServerPath], {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: join(mcpServerPath, ".."),
    });

    this.proc.stdout!.on("data", (chunk: Buffer) =>
      this.onData(chunk.toString())
    );

    // Forward stderr for debugging
    this.proc.stderr!.on("data", (d: Buffer) => {
      process.stderr.write("[mcp] " + d.toString());
    });

    this.proc.on("error", (err) => {
      process.stderr.write("[mcp] spawn error: " + err.message + "\n");
    });

    this.proc.on("exit", (code: number | null) => {
      this._exitCode = code;
      process.stderr.write(`[mcp] process exited with code ${code}\n`);
      // Reject all pending requests
      for (const [id, { reject }] of this.pending) {
        reject(new Error(`MCP process exited (code ${code})`));
      }
      this.pending.clear();
    });

    this._readyPromise = this.handshake();
  }

  private sendLine(line: string) {
    this.proc.stdin!.write(line + "\n");
  }

  private onData(data: string) {
    this.buffer += data;
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg: JsonRpcResponse = JSON.parse(line);
        const pending = this.pending.get(msg.id);
        if (pending) {
          this.pending.delete(msg.id);
          if (msg.error) {
            pending.reject(
              new Error(`MCP error ${msg.error.code}: ${msg.error.message}`)
            );
          } else {
            pending.resolve(msg.result);
          }
        }
      } catch {
        /* skip malformed lines */
      }
    }
  }

  get ready(): boolean {
    return this._ready && this._exitCode === null;
  }

  private async handshake(): Promise<void> {
    // Send initialize request
    const initResult = await this.sendRequest("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "gitgo-dashboard", version: "1.0.0" },
    });
    process.stderr.write(
      `[mcp] handshake ok: server=${initResult?.serverInfo?.name || "?"}\n`
    );

    // Send initialized notification (JSON-RPC notification = no id field)
    this.sendLine(
      JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/initialized",
      })
    );

    this._ready = true;
  }

  private sendRequest(method: string, params: any): Promise<any> {
    if (this._exitCode !== null) {
      return Promise.reject(
        new Error(`MCP process already exited (code ${this._exitCode})`)
      );
    }
    const id = ++this.requestId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      const request: JsonRpcRequest = {
        jsonrpc: "2.0",
        id,
        method,
        params,
      };
      this.proc.stdin!.write(JSON.stringify(request) + "\n");

      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`MCP timeout: ${method}`));
        }
      }, 30000);
    });
  }

  async callTool(
    toolName: string,
    args: Record<string, any> = {}
  ): Promise<any> {
    if (!this._ready) {
      try {
        await this._readyPromise;
      } catch (e: any) {
        throw new Error(`MCP handshake failed: ${e.message}`);
      }
    }

    // FastMCP rejects empty arguments object — omit it when empty
    const params: any = { name: toolName };
    if (Object.keys(args).length > 0) {
      params.arguments = args;
    }

    const result = await this.sendRequest("tools/call", params);

    // FastMCP returns: { content: [{type:'text',text:...}, ...], structuredContent?: {result: [...]} }
    // Use structuredContent.result if available (aggregated array), otherwise merge text entries
    if (result?.structuredContent?.result !== undefined) {
      return result.structuredContent.result;
    }

    const content = result?.content;
    if (content && Array.isArray(content) && content.length > 0) {
      // Multiple text entries — parse each as JSON
      const texts = content
        .filter((c: any) => c.type === "text")
        .map((c: any) => {
          try { return JSON.parse(c.text); } catch { return c.text; }
        });
      if (texts.length === 1) return texts[0];
      // Multi-entry list (e.g. list_projects returns one text per project)
      if (texts.every((t: unknown) => typeof t === "object")) return texts;
      return texts.join("");
    }
    return result;
  }

  close() {
    this.proc.kill();
  }
}
