// src/mock/mockStream.ts — Simulated token streaming for --mock mode.
// Drives the exact same transient streaming row + tool-card lifecycle that the
// native daemon path produces, so the anti-stale rendering is demoed end-to-end.

import type { ToolCallCard, StreamingRow } from "../types.js";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const EDIT_DIFF = `diff --git a/src/api/health.py b/src/api/health.py
--- a/src/api/health.py
+++ b/src/api/health.py
@@ -10,7 +10,9 @@
 async def health():
-    db_ok = await db.ping()
+    db_ok = await db.ping(timeout=config.DB_PING_TIMEOUT_MS)
     if not db_ok:
         return JSONResponse({"code": "db_down", "detail": "db ping failed"}, status_code=503)
+    return {"status": "ok", "db": "up", "uptime": uptime()}
`;

export async function simulateMockStream(opts: {
  onStream: (row: StreamingRow) => void;
  onDone: (finalText: string, tools: ToolCallCard[]) => void;
}): Promise<void> {
  const startTime = new Date().toISOString();
  const tokens = [
    "I'll", " amend", " the", " coder's", " implementation", " to", " wire",
    " the", " ping", " timeout", " to", " config,", " then", " add", " the",
    " 503", " failure-path", " test.", " Editing", " the", " route", " now.",
  ];

  let text = "";
  for (const tok of tokens) {
    text += tok;
    opts.onStream({ text, tools: [], timestamp: startTime });
    await sleep(55);
  }

  // Tool call starts running.
  const runningTool: ToolCallCard = {
    tool_name: "edit",
    target: "src/api/health.py",
    allowed: true,
    status_label: "running",
    duration_ms: 0,
    timestamp: new Date().toISOString(),
    is_running: true,
    state: "running",
  };
  opts.onStream({ text, tools: [runningTool], timestamp: startTime });
  await sleep(420);

  // Tool completes with a result + side-by-side diff.
  const doneTool: ToolCallCard = {
    ...runningTool,
    is_running: false,
    state: "completed",
    status_label: "done",
    duration_ms: 140,
    result_text: "Timeout now configurable; 503 on ping failure",
    diff: EDIT_DIFF,
  };
  opts.onStream({ text, tools: [doneTool], timestamp: startTime });
  await sleep(320);

  const finalText =
    text +
    " Done — the ping timeout now reads from config everywhere, and the 503 failure path is covered by a new test.";
  opts.onDone(finalText, [doneTool]);
}
