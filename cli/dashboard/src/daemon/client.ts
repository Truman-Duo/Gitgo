// src/daemon/client.ts — native daemon client (stdin/stdout line-delimited JSON)
// Communicates directly with gitgo daemon, bypassing MCP.
// Used when Dashboard runs with --native flag.

import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
import type { StreamEvent } from "./streamEvents.js";

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
  // v0.44: streaming callbacks
  private _streamCallbacks = new Map<
    string,
    { onChunk: (event: StreamEvent) => void }
  >();
  private agentSessions = new Map<string, string>();
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

    process.stderr.write(`[daemon] Starting daemon for '${this.projectName}'...\n`);

    // Use python -c (like daemon.bat) to avoid -m gitgo module lookup
    // which depends on the directory being named "gitgo"
    const pythonCode = [
      "import sys",
      `sys.path.insert(0, ${JSON.stringify(gitgoDir)})`,
      "from backend.core.config import ConfigManager",
      "from backend.core.daemon import run_daemon",
      "cfg = ConfigManager.load()",
      `proj = next(p for p in cfg.projects if p.name == ${JSON.stringify(this.projectName)})`,
      "run_daemon(cfg, proj, trial_interval=9999, debounce_sec=2.0)",
    ].join("; ");

    this.proc = spawn(this.pythonPath, ["-u", "-c", pythonCode], {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: gitgoDir,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
      },
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
    const taskId = cmd.task_id || `task_${Date.now()}_${++this.requestId}`;
    const command = { ...cmd, task_id: taskId };
    return new Promise((resolve, reject) => {
      let acknowledged = false;
      let completion: any = undefined;
      let settled = false;

      const succeed = (event: any) => {
        completion = event;
        if (acknowledged && !settled) {
          settled = true;
          resolve(event);
        }
      };
      const fail = (error: Error) => {
        if (!settled) {
          settled = true;
          this.agentPending.delete(taskId);
          reject(error);
        }
      };

      this.agentPending.set(taskId, { resolve: succeed, reject: fail });
      this.sendCommand(command).then((ack) => {
        if (!ack?.process_id || ack?.task_id !== taskId) {
          fail(new Error(`invalid task acknowledgement: ${JSON.stringify(ack)}`));
          return;
        }
        acknowledged = true;
        if (completion !== undefined) succeed(completion);
      }).catch((error) => fail(error instanceof Error ? error : new Error(String(error))));

      setTimeout(() => {
        if (!settled) {
          fail(new Error(`Agent task ${taskId} timed out`));
        }
      }, timeout * 1000);
    });
  }

  // v0.44: streaming task — onChunk receives text_delta/toolcall_start/etc.
  async sendTaskStreaming(
    cmd: Record<string, any>,
    callbacks: {
      onChunk: (event: StreamEvent) => void;
      onAcknowledged?: (ack: any) => void;
      onComplete: (result: any) => void;
      onError: (error: Error) => void;
    },
    timeout = 300,
  ): Promise<void> {
    const taskId = cmd.task_id || `task_${Date.now()}_${++this.requestId}`;
    const command = { ...cmd, task_id: taskId };
    let acknowledged = false;
    let completion: any = undefined;
    let settled = false;

    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      this.agentPending.delete(taskId);
      this._streamCallbacks.delete(taskId);
      callbacks.onError(error);
    };
    const complete = (event: any) => {
      completion = event;
      if (!acknowledged || settled) return;
      settled = true;
      this._streamCallbacks.delete(taskId);
      callbacks.onComplete(event);
    };

    this._streamCallbacks.set(taskId, { onChunk: callbacks.onChunk });
    this.agentPending.set(taskId, { resolve: complete, reject: fail });
    try {
      const ack = await this.sendCommand(command);
      if (!ack?.process_id || ack?.task_id !== taskId) {
        fail(new Error(`invalid task acknowledgement: ${JSON.stringify(ack)}`));
        return;
      }
      acknowledged = true;
      callbacks.onAcknowledged?.(ack);
      if (completion !== undefined) complete(completion);
    } catch (error) {
      fail(error instanceof Error ? error : new Error(String(error)));
      return;
    }

    setTimeout(() => {
      if (!settled) {
        fail(new Error(`Agent task ${taskId} timed out`));
      }
    }, timeout * 1000);
  }

  // ── MCP-compatible interface (drop-in for hooks) ───────────

  async callTool(toolName: string, args: Record<string, any> = {}): Promise<any> {
    switch (toolName) {
      case "gitgo_loop_status": {
        const r = await this.sendCommand({ cmd: "task", action: "status" });
        return { ...args, ...r };
      }

      case "gitgo_agent_chat": {
        const sessionKey = String(args.project || this.projectName);
        const sessionId = this.agentSessions.get(sessionKey);
        const command: Record<string, any> = {
          cmd: "task", action: "chat",
          instruction: args.message || "",
          role: "supervisor",
          actor_kind: "supervisor",
          // Public chat starts with the stable supervisor tool surface. The
          // Host promotes task semantics from committed workflow receipts,
          // never from wording in the user's message.
          capability_profile_id: "supervisor.control",
          task_kind: "answer",
          max_steps: 50,
          task_description: (args.message || "").slice(0, 200),
        };
        if (sessionId) command.session_id = sessionId;
        let r: any;
        try {
          r = await this.sendTask(command, args.timeout || 300);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          if (!sessionId || !message.includes("Session not found:")) throw error;
          // A daemon restart invalidates its in-memory session table. The
          // rejected command has not executed, so one fresh-session retry is
          // safe and prevents a stale client handle from failing forever.
          this.agentSessions.delete(sessionKey);
          delete command.session_id;
          r = await this.sendTask(command, args.timeout || 300);
        }

        if (r?.session_id) {
          this.agentSessions.set(sessionKey, String(r.session_id));
        }

        const outcome = r?.outcome;
        if (!outcome) throw new Error("agent_complete missing TaskOutcome");
        return {
          project: args.project,
          task_id: outcome.task_id,
          process_id: outcome.process_id,
          response: outcome.response || "",
          status: outcome.status,
          error: outcome.error,
          steps_used: outcome.steps_used || 0,
          llm_used: Boolean(outcome.llm_used),
          outcome,
        };
      }

      case "gitgo_stop_process": {
        const r = await this.sendCommand({
          cmd: "task",
          action: "kill",
          process_id: args.process_id || "",
        });
        return { ...args, ...r };
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

    // v0.44: streaming events — route to per-process callback
    if (
      type === "text_delta" ||
      type === "reasoning_delta" ||
      type === "toolcall_start" ||
      type === "toolcall_delta" ||
      type === "tool_progress" ||
      type === "tool_result" ||
      type === "stream_recovery" ||
      type === "provider_request_started" ||
      type === "provider_response_completed" ||
      type === "provider_usage" ||
      type === "governance_snapshot" ||
      type === "context_window_action" ||
      type === "context_compaction_completed" ||
      type === "completion_gate" ||
      type === "agent_started" ||
      type === "agent_terminal" ||
      type === "task_bundle_delegated" ||
      type === "toolcall_done"
    ) {
      const taskId = event.task_id || event.process_id;
      const cb = taskId ? this._streamCallbacks.get(taskId) : undefined;
      if (cb) cb.onChunk(event as StreamEvent);
      return;
    }

    if (type === "agent_complete") {
      const taskId = event.task_id || event.process_id;
      // Clean up streaming callback
      if (taskId && this.agentPending.has(taskId)) {
        const { resolve } = this.agentPending.get(taskId)!;
        this.agentPending.delete(taskId);
        resolve(event);
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
