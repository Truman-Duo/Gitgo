import { describe, expect, test } from "bun:test";
import { buildBtwSources } from "./BtwPanel.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";

function process(
  process_id: string, parent_id: string | null, created_at: string,
  archived = false,
): ProcessInfo {
  return {
    process_id, parent_id, created_at, archived,
    role: parent_id ? "executor" : "supervisor", ring_level: parent_id ? 3 : 0,
    status: "completed", steps_used: 1, max_steps: 8, worktree_path: "",
    provider_id: "provider", model_id: "model", estimated_tokens: 10,
  };
}

describe("BTW multi-agent source projection", () => {
  test("keeps one relevant A, all visible B, and excludes archived processes", () => {
    const rows = {
      oldA: process("oldA", null, "2026-01-01"),
      currentA: process("currentA", null, "2026-01-02"),
      b2: process("b2", "currentA", "2026-01-04"),
      b1: process("b1", "currentA", "2026-01-03"),
      hidden: process("hidden", "currentA", "2026-01-05", true),
    };
    expect(buildBtwSources(rows, "currentA").map((item) => item.process_id))
      .toEqual(["currentA", "b1", "b2"]);
  });

  test("uses the latest A when the current view is a B", () => {
    const rows = {
      a1: process("a1", null, "2026-01-01"),
      a2: process("a2", null, "2026-01-02"),
      b1: process("b1", "a2", "2026-01-03"),
    };
    expect(buildBtwSources(rows, "b1").map((item) => item.process_id))
      .toEqual(["a2", "b1"]);
  });
});
