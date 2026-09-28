// Native Gitgo Host client. This is the Dashboard's production transport.

import { spawn, type ChildProcess } from "node:child_process";
import { dirname } from "node:path";
import { StringDecoder } from "node:string_decoder";
import type { StreamEvent } from "../daemon/streamEvents.js";
import type { PendingDecision } from "../types.js";

export const NATIVE_PROTOCOL_VERSION = 1;

export interface BackendClient {
  readonly ready: boolean;
  callTool(
    operation: string,
    args?: Record<string, any>,
    timeoutSec?: number,
    onEvent?: (event: Record<string, any>) => void,
  ): Promise<any>;
  close(): Promise<void> | void;
  subscribeEvents?(listener: (event: Record<string, any>) => void): () => void;
}

type PendingRequest = {
  operation: string;
  resolve: (value: any) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  expire: () => void;
  timeoutSeconds: number;
  onEvent?: (event: Record<string, any>) => void;
};

export type NativeTaskRequest = {
  project: string;
  message: string;
  max_steps?: number;
  task_kind?: string;
  manual_delegation?: boolean;
  /** Explicit decision identity; ordinary chat can never be silently reclassified. */
  decision?: Pick<PendingDecision, "task_id" | "process_id" | "decision_id">;
};

export function buildNativeTaskCall(args: NativeTaskRequest): {
  operation: "runtime.chat" | "runtime.decision";
  arguments: Record<string, unknown>;
} {
  const { decision, ...taskArgs } = args;
  return decision
    ? { operation: "runtime.decision", arguments: { ...taskArgs, ...decision } }
    : { operation: "runtime.chat", arguments: taskArgs };
}

export function nativeHostCommand(
  pythonPath: string, hostExecutable = "",
): {command: string; args: string[]} {
  const installed = hostExecutable.trim();
  return installed
    ? {command: installed, args: []}
    : {command: pythonPath, args: ["-u", "-m", "backend.core.native_host"]};
}

export class NativeHostClient implements BackendClient {
  private proc: ChildProcess | null = null;
  private requestCounter = 0;
  private pending = new Map<string, PendingRequest>();
  private buffer = "";
  private stdoutDecoder = new StringDecoder("utf8");
  private _ready = false;
  private startResolve?: () => void;
  private startReject?: (error: Error) => void;
  private startTimer?: ReturnType<typeof setTimeout>;
  private eventListeners = new Set<(event: Record<string, any>) => void>();
  constructor(
    private pythonPath: string,
    private gitgoDir: string,
    private hostExecutable = process.env.GITGO_HOST_EXECUTABLE || "",
  ) {}

  get ready(): boolean {
    return this._ready && Boolean(this.proc && this.proc.exitCode === null);
  }

  subscribeEvents(listener: (event: Record<string, any>) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  async start(): Promise<void> {
    if (this.proc) return;
    const installedHost = this.hostExecutable.trim();
    const {command, args} = nativeHostCommand(this.pythonPath, installedHost);
    this.proc = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: installedHost ? dirname(installedHost) : this.gitgoDir,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
      },
      windowsHide: true,
    });
    this.proc.stdout!.on("data", (chunk: Buffer) => this.onData(this.stdoutDecoder.write(chunk)));
    this.proc.stderr!.on("data", (chunk: Buffer) => {
      process.stderr.write("[native-host] " + chunk.toString());
    });
    this.proc.on("error", (error) => this.failAll(error));
    this.proc.on("exit", (code, signal) => {
      this._ready = false;
      this.failAll(new Error(`Native host exited (code=${code}, signal=${signal})`));
    });

    try {
      await new Promise<void>((resolve, reject) => {
        this.startResolve = resolve;
        this.startReject = reject;
        this.startTimer = setTimeout(() => {
          reject(new Error("Native host did not complete its protocol handshake within 30s"));
        }, 30000);
      });

      const capabilities = await this.callTool("host.capabilities", {}, 10);
      if (capabilities?.protocol_version !== NATIVE_PROTOCOL_VERSION) {
        throw new Error(`Native host capability mismatch: ${JSON.stringify(capabilities)}`);
      }
    } catch (error) {
      await this.close();
      throw error;
    }
  }

  callTool(
    operation: string,
    args: Record<string, any> = {},
    timeoutSec = 30,
    onEvent?: (event: Record<string, any>) => void,
  ): Promise<any> {
    return this.request(operation, args, timeoutSec, onEvent);
  }

  private request(
    operation: string,
    args: Record<string, any>,
    timeoutSec: number,
    onEvent?: (event: Record<string, any>) => void,
  ): Promise<any> {
    if (!this.ready || !this.proc?.stdin) {
      return Promise.reject(new Error("Native host is not ready"));
    }
    const requestId = `dashboard_${Date.now()}_${++this.requestCounter}`;
    return new Promise((resolve, reject) => {
      const expire = () => {
        const pending = this.pending.get(requestId);
        if (!pending) return;
        this.pending.delete(requestId);
        if (operation !== "host.cancel") {
          void this.request("host.cancel", { request_id: requestId }, 10).catch(() => undefined);
        }
        reject(new Error(`Native operation '${operation}' timed out after ${pending.timeoutSeconds}s`));
      };
      const timer = setTimeout(expire, timeoutSec * 1000);
      this.pending.set(requestId, { operation, resolve, reject, timer, expire, timeoutSeconds: timeoutSec, onEvent });
      this.proc!.stdin!.write(JSON.stringify({
        protocol_version: NATIVE_PROTOCOL_VERSION,
        type: "request",
        request_id: requestId,
        operation,
        arguments: args,
      }) + "\n");
    });
  }

  async sendTaskStreaming(
    args: NativeTaskRequest,
    callbacks: {
      onChunk: (event: StreamEvent) => void;
      onAcknowledged?: (ack: any) => void;
      onComplete: (result: any) => void;
      onError: (error: Error) => void;
    },
    timeoutSec = 330,
  ): Promise<void> {
    try {
      const call = buildNativeTaskCall(args);
      const result = await this.request(call.operation, call.arguments, timeoutSec, (event) => {
        if (event.event === "runtime_ack") callbacks.onAcknowledged?.(event);
        else callbacks.onChunk(event as StreamEvent);
      });
      callbacks.onComplete({ outcome: result.outcome, ...result });
    } catch (error) {
      callbacks.onError(error instanceof Error ? error : new Error(String(error)));
    }
  }

  private onData(data: string): void {
    this.buffer += data;
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let message: any;
      try {
        message = JSON.parse(line);
      } catch (error) {
        process.stderr.write(`[native-host] malformed protocol line: ${String(error)}\n`);
        continue;
      }
      if (message.protocol_version !== NATIVE_PROTOCOL_VERSION) {
        this.failAll(new Error(`Native protocol version mismatch: ${message.protocol_version}`));
        continue;
      }
      if (message.type === "host_started") {
        if (this.startTimer) clearTimeout(this.startTimer);
        this._ready = true;
        this.startResolve?.();
        this.startResolve = undefined;
        this.startReject = undefined;
        continue;
      }
      if (message.type === "protocol_error") {
        process.stderr.write(`[native-host] protocol error: ${JSON.stringify(message.error)}\n`);
        continue;
      }
      const requestId = String(message.request_id || "");
      const pending = this.pending.get(requestId);
      const eventPayload = message.type === "event" ? {
        ...(message.payload || {}),
        ...(message.project ? { project: message.project } : {}),
        ...(requestId ? { request_id: requestId } : {}),
      } : null;
      if (!pending) {
        if (eventPayload) for (const listener of this.eventListeners) listener(eventPayload);
        continue;
      }
      if (message.type === "event") {
        // Only the correlated Host acknowledgement may set the transport wait
        // budget. Ordinary token/heartbeat traffic cannot renew it forever.
        const wait = Number(message.payload?.wait_timeout_seconds);
        if (message.payload?.event === "runtime_ack" && Number.isFinite(wait) && wait > 0 && wait <= 86430) {
          clearTimeout(pending.timer);
          pending.timeoutSeconds = wait;
          pending.timer = setTimeout(pending.expire, wait * 1000);
        }
        pending.onEvent?.(eventPayload || {});
        // A daemon event is one canonical fact with two audiences: the
        // request-local stream and application read-model subscribers.
        for (const listener of this.eventListeners) listener(eventPayload || {});
        continue;
      }
      if (message.type === "response") {
        clearTimeout(pending.timer);
        this.pending.delete(requestId);
        if (message.ok) pending.resolve(message.result);
        else {
          const error = message.error || {};
          pending.reject(new Error(`${error.code || "NATIVE_ERROR"}: ${error.message || "Unknown error"}`));
        }
      }
    }
  }

  private failAll(error: Error): void {
    if (this.startTimer) clearTimeout(this.startTimer);
    this.startReject?.(error);
    this.startResolve = undefined;
    this.startReject = undefined;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  async close(): Promise<void> {
    const proc = this.proc;
    if (!proc) return;
    // Cancel every paid/runtime request while the native protocol is still
    // writable. Native Host maps each request to its root task or BTW sidecar;
    // daemon shutdown remains the recursive fallback if an acknowledgement is
    // lost during terminal teardown.
    const activeRequestIds = [...this.pending.entries()]
      .filter(([, pending]) => [
        "runtime.chat", "runtime.decision", "runtime.recovery.resume", "runtime.btw",
      ].includes(pending.operation))
      .map(([requestId]) => requestId);
    if (this.ready && activeRequestIds.length > 0) {
      await Promise.race([
        Promise.all(activeRequestIds.map((requestId) =>
          this.request("host.cancel", { request_id: requestId }, 5).catch(() => undefined),
        )),
        new Promise((resolve) => setTimeout(resolve, 2500)),
      ]);
    }
    this.proc = null;
    this._ready = false;
    proc.stdin?.end();
    await new Promise<void>((resolve) => {
      if (proc.exitCode !== null) return resolve();
      const timer = setTimeout(() => {
        proc.kill();
        resolve();
      }, 5000);
      proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}
