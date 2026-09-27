import { describe, expect, test } from "bun:test";
import { buildTopology, isBProcess, visibleBProcesses } from "./ProcessList.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";

function process(id: string, parents: string[]): ProcessInfo {
  return {
    process_id: id,
    role: "worker",
    ring_level: 3,
    status: "running",
    steps_used: 0,
    max_steps: 10,
    parent_id: parents[0] || null,
    parent_ids: parents,
    created_at: `2026-01-01T00:00:0${id.length}Z`,
    worktree_path: "",
    provider_id: "",
    model_id: "",
    estimated_tokens: 0,
  };
}

describe("agent topology projection", () => {
  test("archive hides only the selected B without changing the DAG or runtime state", () => {
    const archived = {...process("b", ["a"]), archived: true};
    const sibling = process("c", ["a"]);
    expect(visibleBProcesses({b: archived, c: sibling}).map(p => p.process_id)).toEqual(["c"]);
    expect(archived.status).toBe("running");
    expect(archived.parent_id).toBe("a");
  });
  test("includes every B state and excludes A supervisors", () => {
    const supervisor = {
      ...process("root", []), actor_kind: "supervisor", role: "supervisor",
    };
    const completedWorker = {
      ...process("old-b", ["old-root"]), actor_kind: "worker", status: "completed",
    };
    const waitingReviewer = {
      ...process("review-b", ["root"]), actor_kind: "reviewer", status: "waiting",
    };
    expect(isBProcess(supervisor)).toBe(false);
    expect(isBProcess(completedWorker)).toBe(true);
    expect(isBProcess(waitingReviewer)).toBe(true);
  });

  test("orders a multi-parent DAG after all dependencies", () => {
    const nodes = buildTopology({
      a: process("a", []),
      b: process("b", ["a"]),
      c: process("c", ["a"]),
      d: process("d", ["b", "c"]),
    });
    const ids = nodes.map((item) => item.process_id);
    expect(ids.indexOf("d")).toBeGreaterThan(ids.indexOf("b"));
    expect(ids.indexOf("d")).toBeGreaterThan(ids.indexOf("c"));
    expect(nodes.find((item) => item.process_id === "d")?.dependency_ids).toEqual(["b", "c"]);
  });

  test("marks cycles instead of recursing forever", () => {
    const nodes = buildTopology({ a: process("a", ["b"]), b: process("b", ["a"]) });
    expect(nodes).toHaveLength(2);
    expect(nodes.every((item) => item.cyclic)).toBe(true);
  });
});
