// src/daemon/client.ts — native daemon client (stdin/stdout line-delimited JSON)
// Communicates directly with gitgo daemon, bypassing MCP.
// Used when Dashboard runs with --native flag.

import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";

export class DaemonClient {
  private proc: ChildProcess | null = null;
  private requestId = 0;
  private pending = new Map<
    string,
    { resolve: Function; reject: Function }
  >();
  private agentPending = new Map<
    string,
    { resolve: Function; reject: Function }
  >();
  private buffer = "";
  private _ready = false;
  private _startedEvent = false;
  private _running = false;

  constructor(
    private projectName: string,
    private pythonPath: string,
  ) {}

  // ── lifecycle ──────────────────────────────────────────────

  async start(): Promise<void> {
    const gitgoDir = resolve(import.meta.dir, "../../../..");
    const daemonArgs = [
      "-m", "gitgo",
      "--mode", "daemon",
      "--project", this.projectName,
      "--daemon-action", "start",
      "--trial-interval", "9999",
      "--debounce", "2.0",
    ];

    process.stderr.write(`[daemon] Starting daemon for '${this.projectName}'...\n`);

    this.proc = spawn(this.pythonPath, daemonArgs, {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: resolve(gitgoDir, ".."),
    });

    this._running = true;

    this.proc.stdout!.on("data", (chunk: Buffer) =>
      this._onData(chunk.toString())
    );

    this.proc.stderr!.on("data", (d: Buffer) => {
      process.stderr.write("[daemon] " + d.toString());
    });

    this.proc.on("error", (err) => {
      process.stderr.write("[daemon] spawn error: " + err.message + "\n");
      this._running = false;
    });

    this.proc.on("exit", (code: number | null) => {
      process.stderr.write(`[daemon] exited code=${code}\n`);
      this._running = false;
      this._ready = false;
      this._wakeAll(new Error(`Daemon exited (code ${code})`));
    });

    // Wait for daemon_started with timeout
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`Daemon for '${this.projectName}' did not start within 30s`));
      }, 30000);

      const check = setInterval(() => {
        if (this._startedEvent) {
          clearTimeout(timeout);
          clearInterval(check);
          this._ready = true;
          process.stderr.write("[daemon] Ready.\n");
          resolve();
        }
      }, 100);
    });
  }

  get ready(): boolean {
    return this._ready && this._running;
  }

  async stop(): Promise<void> {
    if (!this._running || !this.proc) return;
    try {
      this._write({ cmd: "shutdown" });
    } catch { /* ignore */ }
    await new Promise<void>((resolve) => {
      setTimeout(() => {
        if (this.proc) this.proc.kill();
        this._running = false;
        resolve();
      }, 3000);
    });
  }

  close(): void {
    this.stop();
  }

  // ── command interface ──────────────────────────────────────

  async sendCommand(cmd: Record<string, any>, timeout = 30): Promise<any> {
    if (!this._ready) throw new Error("Daemon not ready");

    const requestId = `req_${++this.requestId}`;
    cmd.request_id = requestId;

    return new Promise((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      this._write(cmd);

      setTimeout(() => {
        if (this.pending.has(requestId)) {
          this.pending.delete(requestId);
          reject(new Error(`Command '${cmd.cmd}' timed out`));
        }
      }, timeout * 1000);
    });
  }

  async sendTask(cmd: Record<string, any>, timeout = 300): Promise<any> {
    const ack = await this.sendCommand(cmd);
    const processId = ack?.process_id;
    if (!processId) {
      throw new Error(`task did not return process_id: ${JSON.stringify(ack)}`);
    }

    return new Promise((resolve, reject) => {
      this.agentPending.set(processId, { resolve, reject });

      setTimeout(() => {
        if (this.agentPending.has(processId)) {
          this.agentPending.delete(processId);
          reject(new Error(`Agent task ${processId} timed out`));
        }
      }, timeout * 1000);
    });
  }

  // ── MCP-compatible interface (drop-in for hooks) ───────────

  async callTool(toolName: string, args: Record<string, any> = {}): Promise<any> {
    switch (toolName) {
      case "gitgo_loop_status": {
        const r = await this.sendCommand({ cmd: "task", action: "status" });
        return { ...args, ...r };
      }

      case "gitgo_agent_chat": {
        const r = await this.sendTask({
          cmd: "task", action: "chat",
          instruction: args.message || "",
          role: "executor",
          ring_level: 3,
          max_steps: 50,
          task_description: (args.message || "").slice(0, 200),
        }, args.timeout || 300);

        const resp = r?.result?.response || "";
        return {
          project: args.project,
          process_id: r?.process_id || "",
          response: resp || "(无回复)",
          status: r?.result?.status || "",
          steps_used: r?.result?.steps_used || 0,
          llm_used: true,
        };
      }

      // Non-loop tools not available via daemon — signal caller to use MCP
      default:
        throw new Error(`DAEMON_NO_TOOL:${toolName}`);
    }
  }

  // ── internals ──────────────────────────────────────────────

  private _write(data: Record<string, any>): void {
    if (!this.proc?.stdin) throw new Error("Daemon not running");
    this.proc.stdin.write(JSON.stringify(data) + "\n");
  }

  private _onData(data: string): void {
    this.buffer += data;
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        this._handleEvent(JSON.parse(line));
      } catch {
        /* skip */
      }
    }
  }

  private _handleEvent(event: Record<string, any>): void {
    const type = event.event || "";

    if (type === "daemon_started") {
      this._startedEvent = true;
      return;
    }

    if (type === "command_result") {
      const rid = event.request_id;
      if (rid && this.pending.has(rid)) {
        const { resolve, reject } = this.pending.get(rid)!;
        this.pending.delete(rid);
        event.error ? reject(new Error(event.error)) : resolve(event.result || event);
      }
      return;
    }

    if (type === "agent_complete") {
      const pid = event.process_id;
      if (pid && this.agentPending.has(pid)) {
        const { resolve, reject } = this.agentPending.get(pid)!;
        this.agentPending.delete(pid);
        event.error ? reject(new Error(event.error)) : resolve(event);
      }
      return;
    }
  }

  private _wakeAll(err: Error): void {
    for (const [, p] of this.pending) p.reject(err);
    this.pending.clear();
    for (const [, p] of this.agentPending) p.reject(err);
    this.agentPending.clear();
  }
}
