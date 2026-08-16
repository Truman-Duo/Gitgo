// src/mock/conversations.ts — mock agent conversations (A agent + per-B-agent, running + completed).
import type { ChatMessage, ToolCallCard } from "../types.js";

function card(
  tool_name: string,
  target: string,
  allowed: boolean,
  status_label: string,
  duration_ms: number,
  timestamp: string,
  extra?: Partial<ToolCallCard>,
): ToolCallCard {
  return { tool_name, target, allowed, status_label, duration_ms, timestamp, ...extra };
}

// Workspace A agent conversation (health-check endpoint task)
export const MOCK_MAIN_CONVERSATION: ChatMessage[] = [
  {
    role: "system",
    content: "You are the A agent (orchestrator). Decompose work, dispatch B agents, validate their output, and drive each task to formal sync. Report status at every stage.",
    timestamp: "2026-08-13T08:00:00Z",
  },
  {
    role: "user",
    content: "Add a health check endpoint to the API gateway. It must report liveness, readiness, and database connectivity, and be covered by tests.",
    timestamp: "2026-08-13T08:00:30Z",
  },
  {
    role: "assistant",
    content: "Understood. I'll treat this as a two-stage job: (1) map the existing route layer and DB session wiring, then (2) dispatch a coder B agent to implement GET /health with a db.ping() probe, followed by a reviewer pass. Starting with reconnaissance.",
    timestamp: "2026-08-13T08:01:00Z",
    tools: [
      card("read_file", "src/api/routes.py", true, "done", 45, "08:01:00", { result_text: "200 OK — 85 lines" }),
      card("grep", "health", true, "done", 120, "08:02:00", { result_text: "3 matches in 2 files" }),
      card("read_file", "src/db/session.py", true, "done", 30, "08:02:00", { result_text: "SessionPool + ping() helper present" }),
    ],
  },
  {
    role: "assistant",
    content: "Recon complete. The route layer is registered under /api with a central router, and the DB session module already exposes a ping() helper I can reuse — no new dependency needed. I'll write a precise contract and dispatch the coder.",
    timestamp: "2026-08-13T08:06:00Z",
    tools: [
      card("write_file", "docs/contract/health.md", true, "done", 180, "08:06:00", { result_text: "Contract written — response schema + 200/503 semantics", diff: "diff --git a/docs/contract/health.md b/docs/contract/health.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/contract/health.md\n@@ -0,0 +1,6 @@\n+# Health Endpoint Contract\n+GET /health returns 200 {status, db, uptime} on healthy\n+Failed db.ping() returns 503 {code, detail, retry_after}\n" }),
      card("dispatch_tool", "coder: implement /health", true, "running", 0, "08:06:00", { is_running: true, result_text: "forked proc-002" }),
    ],
  },
  {
    role: "user",
    content: "Make sure it reports database connectivity too, and that a failed DB ping returns 503 rather than 200.",
    timestamp: "2026-08-13T08:07:00Z",
  },
  {
    role: "assistant",
    content: "Noted — I've amended the contract: /health returns 200 only when the DB ping succeeds; a failed ping short-circuits to 503 with a structured error body. The coder is already working against the updated schema, and I'll have the reviewer verify the 503 path explicitly.",
    timestamp: "2026-08-13T08:08:00Z",
    tools: [
      card("edit", "docs/contract/health.md", true, "done", 90, "08:08:00", { result_text: "Added 503 semantics + error body", diff: "diff --git a/docs/contract/health.md b/docs/contract/health.md\n--- a/docs/contract/health.md\n+++ b/docs/contract/health.md\n@@ -2,4 +2,6 @@\n GET /health returns:\n-200 {status, db, uptime}\n+200 {status, db, uptime} on healthy db.ping()\n+503 {code, detail, retry_after} on failed db.ping()\n" }),
    ],
  },
  {
    role: "assistant",
    content: "Coder reports implementation done with 3 passing tests. I'm dispatching a reviewer B agent to audit the diff against the contract, with special attention to the DB-ping failure path and logging format.",
    timestamp: "2026-08-13T08:10:00Z",
    tools: [
      card("dispatch_tool", "reviewer: audit /health diff", true, "running", 0, "08:10:00", { is_running: true, result_text: "forked proc-003" }),
    ],
  },
  {
    role: "assistant",
    content: "Review passed with one nit: the reviewer wants the DB ping timeout made configurable. I've queued that as a follow-up lesson rather than blocking this task, and I'm marking the endpoint ready for formal sync.",
    timestamp: "2026-08-13T08:14:00Z",
    tools: [
      card("bash", "gitgo formal sync ergo", true, "done", 620, "08:14:00", { result_text: "1 commit synced to formal history" }),
    ],
  },
];

// Per-B-agent conversations, keyed by process_id
export const MOCK_AGENT_CONVERSATIONS: Record<string, ChatMessage[]> = {
  "proc-001": [
    {
      role: "system",
      content: "You are the architect B agent. Design and review code changes.",
      timestamp: "2026-08-13T08:00:00Z",
    },
    {
      role: "user",
      content: "Design the health check endpoint contract.",
      timestamp: "2026-08-13T08:00:20Z",
    },
    {
      role: "assistant",
      content: "Proposal: GET /health returns { status, db, uptime }. I'll scan existing route patterns first.",
      timestamp: "2026-08-13T08:01:00Z",
      tools: [
        card("read_file", "src/api/routes.py", true, "done", 45, "08:01:00", { result_text: "200 OK — 85 lines" }),
      ],
    },
    {
      role: "assistant",
      content: "The route layer is a central router under /api. I'll check the DB session helper and existing error conventions before finalizing the response schema.",
      timestamp: "2026-08-13T08:02:00Z",
      tools: [
        card("read_file", "src/db/session.py", true, "done", 30, "08:02:00", { result_text: "SessionPool + ping() helper present" }),
        card("grep", "JSONResponse", true, "done", 70, "08:02:00", { result_text: "4 usages — error shape is { code, detail }" }),
      ],
    },
    {
      role: "user",
      content: "The orchestrator amended the requirement — a failed DB ping must return 503, not 200.",
      timestamp: "2026-08-13T08:07:00Z",
    },
    {
      role: "assistant",
      content: "Updating the schema: 200 only on a successful db.ping(), otherwise 503 with a structured error body. I'll also pin the logging format so the reviewer has a clear spec to check against.",
      timestamp: "2026-08-13T08:08:00Z",
      tools: [
        card("edit", "docs/contract/health.md", true, "done", 90, "08:08:00", { result_text: "Added 503 semantics + error body", diff: "diff --git a/docs/contract/health.md b/docs/contract/health.md\n--- a/docs/contract/health.md\n+++ b/docs/contract/health.md\n@@ -2,4 +2,6 @@\n GET /health returns:\n-200 {status, db, uptime}\n+200 {status, db, uptime} on healthy db.ping()\n+503 {code, detail, retry_after} on failed db.ping()\n" }),
      ],
    },
    {
      role: "assistant",
      content: "Contract frozen as v2: GET /health → 200 {status, db, uptime} on healthy, 503 {code, detail, retry_after} on failed ping. Coder is implementing against v2; I'll re-check the diff before it reaches review.",
      timestamp: "2026-08-13T08:09:00Z",
    },
  ],
  "proc-002": [
    {
      role: "system",
      content: "You are the coder B agent. Implement the assigned task in your worktree.",
      timestamp: "2026-08-13T08:05:00Z",
    },
    {
      role: "user",
      content: "Implement GET /health with a DB ping.",
      timestamp: "2026-08-13T08:05:30Z",
    },
    {
      role: "assistant",
      content: "Writing the endpoint and a db.ping() helper.",
      timestamp: "2026-08-13T08:03:00Z",
      tools: [
        card("write_file", "src/api/health.py", true, "done", 230, "08:03:00", { result_text: "Created — 42 lines", diff: "diff --git a/src/api/health.py b/src/api/health.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/api/health.py\n@@ -0,0 +1,8 @@\n+from fastapi import APIRouter, JSONResponse\n+router = APIRouter()\n+\n+@router.get(\"/health\")\n+async def health():\n+    db_ok = await db.ping()\n+    if not db_ok:\n+        return JSONResponse({\"code\": \"db_down\", \"detail\": \"db ping failed\"}, status_code=503)\n+    return {\"status\": \"ok\", \"db\": \"up\", \"uptime\": uptime()}\n" }),
        card("bash", "pytest tests/api/test_health.py -v", true, "done", 1500, "08:04:00", { result_text: "3 passed in 1.23s" }),
      ],
    },
    {
      role: "assistant",
      content: "Tests pass. Attempting an out-of-workspace write was blocked by policy.",
      timestamp: "2026-08-13T08:05:00Z",
      tools: [
        card("write_file", "/etc/passwd", false, "blocked", 0, "08:05:00", { blocked_reason: "security_policy: writes outside workspace" }),
      ],
    },
    {
      role: "user",
      content: "The contract now requires a configurable ping timeout and a 503 path test. Please add both.",
      timestamp: "2026-08-13T08:06:30Z",
    },
    {
      role: "assistant",
      content: "Adding a DB_PING_TIMEOUT_MS setting read from the app config, then wiring the ping() call to fail into a 503 with the structured error body.",
      timestamp: "2026-08-13T08:07:00Z",
      tools: [
        card("edit", "src/api/health.py", true, "done", 140, "08:07:00", { result_text: "Timeout now configurable; 503 on ping failure", diff: "diff --git a/src/api/health.py b/src/api/health.py\n--- a/src/api/health.py\n+++ b/src/api/health.py\n@@ -10,7 +10,9 @@\n async def health():\n-    db_ok = await db.ping()\n+    db_ok = await db.ping(timeout=config.DB_PING_TIMEOUT_MS)\n     if not db_ok:\n         return JSONResponse({\"code\": \"db_down\", \"detail\": \"db ping failed\"}, status_code=503)\n+    return {\"status\": \"ok\", \"db\": \"up\", \"uptime\": uptime()}\n" }),
        card("write_file", "tests/api/test_health_503.py", true, "done", 210, "08:08:00", { result_text: "Created — 2 failure-path tests", diff: "diff --git a/tests/api/test_health_503.py b/tests/api/test_health_503.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/api/test_health_503.py\n@@ -0,0 +1,5 @@\n+def test_db_down_returns_503():\n+    resp = client.get(\"/health\")\n+    assert resp.status_code == 503\n+    assert resp.json()[\"code\"] == \"db_down\"\n" }),
      ],
    },
    {
      role: "assistant",
      content: "Full suite green — 5 passed. The 503 path returns {code, detail, retry_after} exactly as the v2 contract specifies. Handing off to review.",
      timestamp: "2026-08-13T08:10:00Z",
      tools: [
        card("bash", "pytest tests/api/ -v", true, "done", 1800, "08:09:00", { result_text: "5 passed in 1.87s" }),
      ],
    },
  ],
  "proc-003": [
    {
      role: "system",
      content: "You are the reviewer B agent. Verify coder output against the contract.",
      timestamp: "2026-08-13T08:10:00Z",
    },
    {
      role: "user",
      content: "Review the coder's /health implementation.",
      timestamp: "2026-08-13T08:10:30Z",
    },
    {
      role: "assistant",
      content: "Reviewing diff. Requesting the changed files now.",
      timestamp: "2026-08-13T08:12:00Z",
      tools: [
        card("read_file", "src/api/health.py", true, "done", 60, "08:12:00", { result_text: "42 lines reviewed" }),
      ],
    },
    {
      role: "assistant",
      content: "Checking the failure path against the v2 contract — specifically the 503 error body and whether the timeout is actually wired to config.",
      timestamp: "2026-08-13T08:13:00Z",
      tools: [
        card("read_file", "tests/api/test_health_503.py", true, "done", 50, "08:13:00", { result_text: "2 failure-path tests" }),
        card("grep", "DB_PING_TIMEOUT_MS", true, "done", 65, "08:13:00", { result_text: "1 match in config.py + 1 in health.py" }),
      ],
    },
    {
      role: "assistant",
      content: "Implementation matches the schema. One nit: the ping timeout is hard-coded in one fallback branch — it should read the config everywhere. Approving with a follow-up lesson rather than blocking the task.",
      timestamp: "2026-08-13T08:15:00Z",
      tools: [
        card("bash", "pytest tests/api/ -v", true, "done", 900, "08:15:00", { result_text: "5 passed in 1.81s" }),
      ],
    },
  ],
  "proc-004": [
    {
      role: "system",
      content: "You are the orchestrator B agent. Run the migration task end-to-end.",
      timestamp: "2026-08-13T07:00:00Z",
    },
    {
      role: "user",
      content: "Ship the db-migrations feature.",
      timestamp: "2026-08-13T07:00:20Z",
    },
    {
      role: "assistant",
      content: "Enumerating migration files and applying the schema diff.",
      timestamp: "2026-08-13T07:01:00Z",
      tools: [
        card("glob", "migrations/*.sql", true, "done", 80, "07:01:00", { result_text: "6 files" }),
        card("edit", "migrations/0042_user_schema.sql", true, "done", 95, "07:02:00", { result_text: "Applied" }),
      ],
    },
    {
      role: "assistant",
      content: "Migration complete. 25/25 steps consumed, all synced to formal history.",
      timestamp: "2026-08-13T07:10:00Z",
    },
  ],
  "proc-005": [
    {
      role: "system",
      content: "You are the coder B agent. Implement the assigned task in your worktree.",
      timestamp: "2026-08-13T07:15:00Z",
    },
    {
      role: "user",
      content: "Refactor the logging pipeline.",
      timestamp: "2026-08-13T07:15:20Z",
    },
    {
      role: "assistant",
      content: "Running a migration script that needs elevated shell access.",
      timestamp: "2026-08-13T07:15:30Z",
      tools: [
        card("bash", "sudo ./scripts/migrate.sh", false, "blocked", 0, "07:15:00", { blocked_reason: "identity_guard: 3 consecutive denials" }),
      ],
    },
    {
      role: "assistant",
      content: "Killed after 3 consecutive denials — identity guard tripped.",
      timestamp: "2026-08-13T07:16:00Z",
    },
  ],
  "proc-006": [
    {
      role: "system",
      content: "You are the debugger B agent. Investigate the failure in proc-005.",
      timestamp: "2026-08-13T07:20:00Z",
    },
    {
      role: "user",
      content: "Why did proc-005 get killed?",
      timestamp: "2026-08-13T07:20:20Z",
    },
    {
      role: "assistant",
      content: "Searching denial logs for the identity guard trip.",
      timestamp: "2026-08-13T07:20:30Z",
      tools: [
        card("grep", "identity_guard", true, "done", 40, "07:20:00", { result_text: "3 denials found" }),
      ],
    },
    {
      role: "assistant",
      content: "Root cause: sudo escalation attempt blocked. This process is now orphaned.",
      timestamp: "2026-08-13T07:21:00Z",
    },
  ],
};

// Completed A agent conversation (rate-limiter task)
export const MOCK_MAIN_CONVERSATION_COMPLETED: ChatMessage[] = [
  {
    role: "system",
    content: "You are the A agent (orchestrator). Decompose work, dispatch B agents, validate their output, and drive each task to formal sync. Report status at every stage.",
    timestamp: "2026-08-10T09:00:00Z",
  },
  {
    role: "user",
    content: "Add per-route rate limiting to the API gateway. It must use a token bucket, expose burst capacity, and export quota metrics.",
    timestamp: "2026-08-10T09:00:30Z",
  },
  {
    role: "assistant",
    content: "Three-part job: (1) map the gateway middleware and existing metrics sink, (2) dispatch a coder to implement the token bucket + quota exporter, (3) run a reviewer pass over the burst-control edge cases. Starting reconnaissance.",
    timestamp: "2026-08-10T09:01:00Z",
    tools: [
      card("read_file", "gateway/middleware.go", true, "done", 40, "09:01:00", { result_text: "200 OK — 118 lines" }),
      card("grep", "token_bucket", true, "done", 90, "09:02:00", { result_text: "no existing implementation" }),
    ],
  },
  {
    role: "assistant",
    content: "The gateway middleware chain is clean — no existing limiter, and the metrics sink already exposes a histogram hook I can reuse. Writing a precise contract and dispatching the coder.",
    timestamp: "2026-08-10T09:02:00Z",
    tools: [
      card("write_file", "docs/contract/rate-limiter.md", true, "done", 150, "09:02:00", { result_text: "Contract written — token bucket + burst + metrics", diff: "diff --git a/docs/contract/rate-limiter.md b/docs/contract/rate-limiter.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/contract/rate-limiter.md\n@@ -0,0 +1,6 @@\n+# Rate Limiter Contract\n+Per-route token bucket with configurable limits\n+Atomic refill so a quota reset never drops in-flight requests\n+Expose burst capacity + export quota metrics\n" }),
      card("dispatch_tool", "coder: implement rate-limiter", true, "running", 0, "09:02:00", { is_running: true, result_text: "forked cpl-002" }),
    ],
  },
  {
    role: "user",
    content: "Make the per-route limit configurable, and ensure a quota reset doesn't drop in-flight requests.",
    timestamp: "2026-08-10T09:05:00Z",
  },
  {
    role: "assistant",
    content: "Amending the contract: limits come from route config, and the bucket refill is atomic so a reset never drops in-flight requests. Coder is updating against the v2 schema now.",
    timestamp: "2026-08-10T09:06:00Z",
    tools: [
      card("edit", "docs/contract/rate-limiter.md", true, "done", 80, "09:06:00", { result_text: "Added per-route config + atomic refill", diff: "diff --git a/docs/contract/rate-limiter.md b/docs/contract/rate-limiter.md\n--- a/docs/contract/rate-limiter.md\n+++ b/docs/contract/rate-limiter.md\n@@ -2,3 +2,5 @@\n Per-route token bucket with configurable limits\n-Refill on a fixed interval\n+Atomic refill — quota reset preserves in-flight requests\n+Limits read from route config, not hard-coded\n Expose burst capacity + export quota metrics\n" }),
    ],
  },
  {
    role: "assistant",
    content: "Coder reports implementation done with 6 passing tests. Dispatching a reviewer to audit burst-control and quota-reset edge cases against the contract.",
    timestamp: "2026-08-10T09:40:00Z",
    tools: [
      card("dispatch_tool", "reviewer: audit rate-limiter diff", true, "running", 0, "09:40:00", { is_running: true, result_text: "forked cpl-003" }),
    ],
  },
  {
    role: "assistant",
    content: "Reviewer approved with no blockers. Syncing the feature to formal history and closing the task.",
    timestamp: "2026-08-10T09:50:00Z",
    tools: [
      card("bash", "gitgo formal sync atlas", true, "done", 540, "09:50:00", { result_text: "2 commits synced to formal history" }),
    ],
  },
  {
    role: "assistant",
    content: "Task complete. Rate limiting is live with configurable per-route limits, atomic refill, and quota metrics exported. 41/41 commits synced; no pending lessons remain.",
    timestamp: "2026-08-10T09:52:00Z",
  },
];

// Completed B-agent conversations, keyed by process_id
export const MOCK_AGENT_CONVERSATIONS_COMPLETED: Record<string, ChatMessage[]> = {
  "cpl-001": [
    {
      role: "system",
      content: "You are the orchestrator B agent. Run the assigned feature end-to-end.",
      timestamp: "2026-08-10T09:00:00Z",
    },
    {
      role: "user",
      content: "Ship the rate-limiter feature.",
      timestamp: "2026-08-10T09:00:20Z",
    },
    {
      role: "assistant",
      content: "Enumerating gateway middleware and the metrics sink before dispatching.",
      timestamp: "2026-08-10T09:01:00Z",
      tools: [
        card("glob", "gateway/*.go", true, "done", 70, "09:01:00", { result_text: "9 files" }),
      ],
    },
    {
      role: "assistant",
      content: "Feature shipped. 18/18 steps consumed, all commits synced to formal history.",
      timestamp: "2026-08-10T09:48:00Z",
    },
  ],
  "cpl-002": [
    {
      role: "system",
      content: "You are the coder B agent. Implement the assigned task in your worktree.",
      timestamp: "2026-08-10T09:10:00Z",
    },
    {
      role: "user",
      content: "Implement the token-bucket rate limiter with configurable per-route limits.",
      timestamp: "2026-08-10T09:10:30Z",
    },
    {
      role: "assistant",
      content: "Writing the limiter and wiring it into the gateway middleware.",
      timestamp: "2026-08-10T09:12:00Z",
      tools: [
        card("write_file", "gateway/ratelimit.go", true, "done", 250, "09:12:00", { result_text: "Created — 96 lines", diff: "diff --git a/gateway/ratelimit.go b/gateway/ratelimit.go\nnew file mode 100644\n--- /dev/null\n+++ b/gateway/ratelimit.go\n@@ -0,0 +1,8 @@\n+package gateway\n+\n+type TokenBucket struct {\n+    capacity int\n+    refill   time.Duration\n+}\n+\n+func (b *TokenBucket) Allow() bool { return b.tokens > 0 }\n" }),
        card("bash", "go test ./gateway/ -run Rate -v", true, "done", 1200, "09:13:00", { result_text: "6 passed in 2.1s" }),
      ],
    },
    {
      role: "assistant",
      content: "Atomic refill implemented; quota reset preserves in-flight requests. Done — 22/22 steps consumed.",
      timestamp: "2026-08-10T09:38:00Z",
    },
  ],
  "cpl-003": [
    {
      role: "system",
      content: "You are the reviewer B agent. Verify coder output against the contract.",
      timestamp: "2026-08-10T09:40:00Z",
    },
    {
      role: "user",
      content: "Review the rate-limiter implementation.",
      timestamp: "2026-08-10T09:40:30Z",
    },
    {
      role: "assistant",
      content: "Auditing burst-control and quota-reset edge cases against the v2 contract.",
      timestamp: "2026-08-10T09:42:00Z",
      tools: [
        card("read_file", "gateway/ratelimit.go", true, "done", 55, "09:41:00", { result_text: "96 lines reviewed" }),
        card("bash", "go test ./gateway/ -run Rate -v", true, "done", 900, "09:42:00", { result_text: "6 passed in 2.0s" }),
      ],
    },
    {
      role: "assistant",
      content: "Approved — no blockers. Edge cases handled correctly. 9/9 steps consumed.",
      timestamp: "2026-08-10T09:49:00Z",
    },
  ],
};
