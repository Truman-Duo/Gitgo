import { describe, expect, test } from "bun:test";
import type { ChatMessage } from "../types.js";
import { knowledgeHarvestMessage, reconcileChatMessages } from "./useChat.js";

const msg = (patch: Partial<ChatMessage>): ChatMessage => ({
  role: "user",
  content: "",
  timestamp: "2026-01-01T00:00:00Z",
  ...patch,
});

describe("reconcileChatMessages", () => {
  test("stale polls cannot reopen a Host-accepted decision or erase its answer", () => {
    const pending = msg({message_id: "decision:d1", kind: "decision", role: "assistant",
      decision: {decision_id: "d1", task_id: "t", process_id: "a", question: "Which?",
        why_user_must_decide: "preference", options: [], allow_free_form: true},
      status: "awaiting_user"});
    const answered = {...pending, status: "answered", decision_answer: "Minimal", awaiting_persistence: true};
    const firstPoll = reconcileChatMessages([pending], [answered]);
    const secondPoll = reconcileChatMessages([pending], firstPoll);
    expect(secondPoll[0].decision_answer).toBe("Minimal");
    expect(secondPoll[0].status).toBe("answered");
    expect(secondPoll[0].awaiting_persistence).toBe(true);
    expect(reconcileChatMessages([{...answered, awaiting_persistence: undefined}], secondPoll)[0].awaiting_persistence).toBe(false);
  });

  test("identical questions in successive decisions keep separate identities", () => {
    const question = msg({message_id: "decision:d1", kind: "decision", role: "assistant", content: "Continue?",
      turn_id: "task", decision: {decision_id: "d1", task_id: "task", process_id: "a", question: "Continue?",
        why_user_must_decide: "scope", options: [], allow_free_form: true}});
    const next = {...question, message_id: "decision:d2", decision: {...question.decision!, decision_id: "d2"}, awaiting_persistence: true};
    expect(reconcileChatMessages([question], [next]).map(row => row.message_id)).toEqual(["decision:d1", "decision:d2"]);
  });
  test("replaces matching optimistic rows with one durable row", () => {
    const current = [
      msg({ message_id: "optimistic:1:user", content: "hello" }),
      msg({
        message_id: "optimistic:1:assistant", role: "assistant", content: "done",
        tools: [{ tool_name: "read", target: "a", allowed: true,
          status_label: "done", duration_ms: 1, timestamp: "now" }],
      }),
    ];
    const durable = [
      msg({ message_id: "db:user", content: "hello" }),
      msg({ message_id: "outcome:1", role: "assistant", content: "done" }),
    ];

    const reconciled = reconcileChatMessages(durable, current);

    expect(reconciled).toHaveLength(2);
    expect(reconciled.map((item) => item.message_id)).toEqual(["db:user", "outcome:1"]);
    expect(reconciled[1].tools?.[0]?.tool_name).toBe("read");
  });

  test("keeps an optimistic row while the durable poll is stale", () => {
    const pending = msg({ message_id: "optimistic:2:user", content: "new task" });
    expect(reconcileChatMessages([], [pending])).toEqual([pending]);
  });

  test("drops a frontend-only transport error on the next authoritative poll", () => {
    const transport = msg({
      message_id: "transport-error:2026-01-01T00:00:00Z",
      role: "assistant", kind: "error", content: "[Error: admission timed out]",
      awaiting_persistence: false,
    });
    expect(reconcileChatMessages([], [transport])).toEqual([]);
  });

  test("does not consume a repeated new question with an older identical message", () => {
    const old = msg({ message_id: "old:user", content: "continue" });
    const pending = msg({ message_id: "optimistic:2:user", content: "continue",
      timestamp: "2026-01-01T00:01:00Z" });
    expect(reconcileChatMessages([old], [old, pending])).toEqual([old, pending]);
  });

  test("deduplicates repeated durable message ids", () => {
    const durable = msg({ message_id: "outcome:one", role: "assistant", content: "once" });
    expect(reconcileChatMessages([durable, durable], [])).toEqual([durable]);
  });

  test("keeps the final capsule identity while awaiting persistence", () => {
    const live = msg({ message_id: "outcome:round", turn_id: "round", role: "assistant",
      content: "done", status: "completed", duration_ms: 1500, trace_id: "root-trace",
      awaiting_persistence: true });
    expect(reconcileChatMessages([], [live])).toEqual([live]);
    const durable = { ...live, awaiting_persistence: undefined, duration_ms: 1600 };
    const rows = reconcileChatMessages([durable], [live]);
    expect(rows).toHaveLength(1);
    expect(rows[0].message_id).toBe(live.message_id);
    expect(rows[0].duration_ms).toBe(1600);
    expect(rows[0].awaiting_persistence).toBe(false);
  });

  test("keeps a completed timeline across repeated authoritative polls", () => {
    const durable = msg({message_id: "outcome:t", role: "assistant", content: "done", trace_id: "t"});
    const decorated = {...durable, activity: [{kind: "progress" as const,
      text: "Checked the implementation.", visibility: "public" as const}]};
    const once = reconcileChatMessages([durable], [decorated]);
    const twice = reconcileChatMessages([durable], once);
    expect(twice[0].activity).toEqual(decorated.activity);
  });
});

describe("knowledgeHarvestMessage", () => {
  test("reports newly harvested pending lessons", () => {
    const notice = knowledgeHarvestMessage("project", {
      event: "lessons_harvested", harvest_id: "h1", count: 2,
      time: "2026-01-01T00:00:00Z",
    });
    expect(notice.message_id).toBe("knowledge:project:h1");
    expect(notice.content).toContain("2 pending lessons");
  });

  test("keeps a visible completion notice when semantic deduplication adds nothing", () => {
    const notice = knowledgeHarvestMessage("project", {
      event: "lessons_harvested", harvest_id: "h2", count: 0,
    });
    expect(notice.content).toBe("Knowledge review complete · no new lesson added.");
  });
});
