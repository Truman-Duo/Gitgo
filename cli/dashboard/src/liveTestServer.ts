import { resolve } from "node:path";

import { NativeHostClient } from "./backend/client.js";
import { resolvePythonRuntime } from "./backend/pythonRuntime.js";

const GITGO_DIR = resolve(import.meta.dir, "../../..");
const PYTHON = resolvePythonRuntime();
const HOST = "127.0.0.1";
const PORT = Number(process.env.GITGO_LIVE_PORT || 43121);
const encoder = new TextEncoder();

type LiveEnvelope = {
  kind: "server" | "event" | "complete" | "error";
  at: string;
  payload: Record<string, unknown>;
};

const subscribers = new Set<ReadableStreamDefaultController<Uint8Array>>();
const client = new NativeHostClient(PYTHON, GITGO_DIR);
let running = false;

function publish(kind: LiveEnvelope["kind"], payload: Record<string, unknown>): void {
  const envelope: LiveEnvelope = { kind, at: new Date().toISOString(), payload };
  const bytes = encoder.encode(`data: ${JSON.stringify(envelope)}\n\n`);
  for (const subscriber of [...subscribers]) {
    try {
      subscriber.enqueue(bytes);
    } catch {
      subscribers.delete(subscriber);
    }
  }
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

async function runTask(project: string, message: string): Promise<void> {
  running = true;
  publish("server", { event: "request_started", project, message });
  await client.sendTaskStreaming(
    { project, message, task_kind: "supervisor" },
    {
      onAcknowledged: (ack) => publish("event", { event: "runtime_ack", ...ack }),
      onChunk: (event) => publish("event", event as unknown as Record<string, unknown>),
      onComplete: (result) => {
        publish("complete", result as Record<string, unknown>);
        running = false;
      },
      onError: (error) => {
        publish("error", { event: "request_error", message: error.message });
        running = false;
      },
    },
    330,
  );
}

const PAGE = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gitgo Live Test Console</title>
  <style>
    :root { color-scheme: dark; font-family: ui-monospace, Consolas, monospace; }
    body { margin: 0; background: #101318; color: #e7edf5; }
    header, main { max-width: 1180px; margin: auto; padding: 16px; }
    header { display: flex; align-items: baseline; gap: 16px; border-bottom: 1px solid #303844; }
    h1 { font-size: 20px; margin: 0; }
    .status { color: #9fc5ff; }
    .composer { display: grid; gap: 10px; margin-bottom: 16px; }
    input, textarea, button { font: inherit; color: inherit; background: #171c23; border: 1px solid #3b4654; border-radius: 6px; padding: 9px; }
    textarea { min-height: 110px; resize: vertical; }
    button { width: 160px; cursor: pointer; background: #234a77; }
    button:disabled { opacity: .55; cursor: wait; }
    .toolbar { display: flex; gap: 18px; align-items: center; font-size: 13px; color: #b5c0ce; }
    #events { display: grid; gap: 8px; }
    .entry { border: 1px solid #303844; border-left: 4px solid #56728f; border-radius: 5px; padding: 9px 11px; background: #151a21; }
    .entry.reasoning_chunk { border-left-color: #b084f5; }
    .entry.tool_call, .entry.tool_result, .entry.tool_executed { border-left-color: #efb65c; }
    .entry.completion_gate, .entry.governance_snapshot, .entry.review_child_outcome { border-left-color: #63c6a2; }
    .entry.complete { border-left-color: #62d27f; }
    .entry.error { border-left-color: #ef6b73; }
    .meta { color: #8fa0b3; font-size: 12px; margin-bottom: 5px; }
    .summary { white-space: pre-wrap; overflow-wrap: anywhere; }
    details { margin-top: 6px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; color: #b9c6d6; margin: 6px 0 0; }
    #final { margin: 16px 0; border: 1px solid #47765a; padding: 12px; white-space: pre-wrap; display: none; }
  </style>
</head>
<body>
  <header><h1>Gitgo Live Test Console</h1><span id="status" class="status">正在连接原生 Host…</span></header>
  <main>
    <section class="composer">
      <input id="project" value="gitgo" aria-label="项目">
      <textarea id="message" aria-label="任务" placeholder="输入要交给 Gitgo A 级主管的任务"></textarea>
      <div class="toolbar">
        <button id="send">发送到 Gitgo</button>
        <label><input id="follow" type="checkbox" checked> 自动跟随</label>
        <span id="trace"></span>
      </div>
    </section>
    <section id="final"></section>
    <section id="events" aria-live="polite"></section>
  </main>
  <script>
    const statusEl = document.querySelector('#status');
    const send = document.querySelector('#send');
    const message = document.querySelector('#message');
    const project = document.querySelector('#project');
    const events = document.querySelector('#events');
    const finalEl = document.querySelector('#final');
    const traceEl = document.querySelector('#trace');
    const follow = document.querySelector('#follow');

    function eventName(item) {
      return String(item.payload?.event || item.payload?.type || item.kind || 'event');
    }

    function summary(item) {
      const p = item.payload || {};
      if (item.kind === 'error') return p.message || JSON.stringify(p);
      if (item.kind === 'complete') {
        const o = p.outcome || p;
        return '最终状态: ' + (o.status || 'unknown') + '\\n' + (o.response || p.response || '');
      }
      if (p.event === 'reasoning_chunk' || p.event === 'reasoning_delta') return p.content || p.reasoning || p.delta || p.text || '';
      if (p.event === 'text_chunk' || p.event === 'text_delta') return p.content || p.delta || p.text || '';
      if (p.event === 'tool_call' || p.event === 'toolcall_start') return (p.tool_name || p.name || 'tool') + '  ' + JSON.stringify(p.arguments || p.input || {});
      if (p.event === 'toolcall_delta') return (p.tool_name || 'tool arguments') + '  ' + (p.delta || '');
      if (p.event === 'toolcall_done') return (p.tool_name || 'tool') + '  completed';
      if (p.event === 'tool_result' || p.event === 'tool_executed') return (p.tool_name || p.name || 'tool') + '  ' + (p.output || p.result || p.status || '');
      if (p.event === 'runtime_ack') return 'task=' + (p.task_id || '') + ' process=' + (p.process_id || '');
      if (p.event === 'request_started') return p.message || '';
      return p.summary || p.message || p.status || JSON.stringify(p);
    }

    function append(item) {
      const name = eventName(item);
      const entry = document.createElement('article');
      entry.className = 'entry ' + (item.kind === 'complete' ? 'complete' : item.kind === 'error' ? 'error' : name);
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = item.at + '  ·  ' + name;
      const body = document.createElement('div');
      body.className = 'summary';
      const value = summary(item);
      body.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      const details = document.createElement('details');
      const label = document.createElement('summary');
      label.textContent = '完整事件 JSON';
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(item.payload, null, 2);
      details.append(label, pre);
      entry.append(meta, body, details);
      events.append(entry);
      const p = item.payload || {};
      const traceId = p.trace_id || p.task_id || p.outcome?.trace_id || p.outcome?.task_id;
      if (traceId) traceEl.textContent = 'trace/task: ' + traceId;
      if (item.kind === 'complete' || item.kind === 'error') {
        send.disabled = false;
        statusEl.textContent = item.kind === 'complete' ? '任务完成' : '任务失败';
        finalEl.style.display = 'block';
        finalEl.textContent = body.textContent;
      }
      if (follow.checked) entry.scrollIntoView({ block: 'end' });
    }

    const source = new EventSource('/api/events');
    source.onopen = () => { statusEl.textContent = '原生 Host 已连接'; };
    source.onerror = () => { statusEl.textContent = '事件流连接中断，正在重连'; };
    source.onmessage = (event) => append(JSON.parse(event.data));

    send.addEventListener('click', async () => {
      const text = message.value.trim();
      if (!text) return;
      send.disabled = true;
      finalEl.style.display = 'none';
      events.replaceChildren();
      traceEl.textContent = '';
      statusEl.textContent = '正在执行…';
      const response = await fetch('/api/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: project.value.trim() || 'gitgo', message: text }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        append({ kind: 'error', at: new Date().toISOString(), payload: { message: error.error || response.statusText } });
      }
    });
  </script>
</body>
</html>`;

await client.start();

const server = Bun.serve({
  hostname: HOST,
  port: PORT,
  idleTimeout: 255,
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      return new Response(PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
    if (url.pathname === "/api/status") {
      return json({ ready: client.ready, running, project: "gitgo" });
    }
    if (url.pathname === "/api/events") {
      let controllerRef: ReadableStreamDefaultController<Uint8Array> | undefined;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controllerRef = controller;
          subscribers.add(controller);
          controller.enqueue(encoder.encode(": connected\n\n"));
        },
        cancel() {
          if (controllerRef) subscribers.delete(controllerRef);
        },
      });
      return new Response(stream, {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache, no-store",
          "Connection": "keep-alive",
          "X-Content-Type-Options": "nosniff",
        },
      });
    }
    if (url.pathname === "/api/send" && request.method === "POST") {
      if (running) return json({ error: "已有任务正在执行" }, 409);
      const body = await request.json().catch(() => ({})) as Record<string, unknown>;
      const project = String(body.project || "gitgo").trim();
      const message = String(body.message || "").trim();
      if (!message) return json({ error: "任务内容不能为空" }, 400);
      void runTask(project, message).catch((error) => {
        running = false;
        publish("error", {
          event: "request_error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
      return json({ accepted: true }, 202);
    }
    return new Response("Not found", { status: 404 });
  },
});

publish("server", { event: "server_ready", url: server.url.href });
process.stdout.write(`[gitgo-live] ${server.url.href}\n`);

async function shutdown(): Promise<void> {
  server.stop(true);
  await client.close();
  process.exit(0);
}

process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());
