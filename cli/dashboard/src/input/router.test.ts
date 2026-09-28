import { describe, expect, test } from "bun:test";
import { InputRouter, InputBindingRegistry } from "./router.js";
import { runningWorkers, agentLabel } from "../daemon/agentLabels.js";

describe("central input ownership", () => {
  test("overlay scroll owns wheel and paging while other keys reach one modal", () => {
    const router = new InputRouter();
    const calls: string[] = [];
    let overlay = true;
    router.register({ id: "scene", priority: 0, enabled: () => true, handle: () => { calls.push("scene"); return true; } });
    router.register({ id: "dialog", priority: 100, enabled: () => overlay, handle: () => { calls.push("dialog"); return true; } });
    router.register({ id: "scroll", priority: 150, enabled: () => overlay, handle: ({key}) => !!(key.wheelUp || key.pageDown) });
    expect(router.dispatch({input: "", key: {wheelUp: true}})).toBe("scroll");
    expect(router.dispatch({input: "", key: {pageDown: true}})).toBe("scroll");
    expect(calls).toEqual([]);
    expect(router.dispatch({input: "", key: {tab: true}})).toBe("dialog");
    overlay = false;
    expect(router.dispatch({input: "", key: {tab: true}})).toBe("scene");
    expect(calls).toEqual(["dialog", "scene"]);
  });
  test("rebinds atomically, rejects conflicts, removes displaced defaults", () => {
    const registry = new InputBindingRegistry();
    registry.configure({version: 1, overrides: {"execution.interrupt": {key: "x", ctrl: true}}});
    expect(registry.normalize({input: "x", key: {ctrl: true}}).key.escape).toBe(true);
    expect(registry.normalize({input: "", key: {escape: true, meta: true}}).key.escape).toBeUndefined();
    expect(() => registry.configure({version: 1, overrides: {"execution.interrupt": {key: "c", ctrl: true}}})).toThrow("CONFLICT");
    expect(registry.snapshot().overrides["execution.interrupt"]).toEqual({key: "x", ctrl: true});
  });
  test("recognizes Ink Escape metadata without rewriting paste to actions", () => {
    const registry = new InputBindingRegistry();
    expect(registry.normalize({input: "", key: {escape: true, meta: true}}).key.escape).toBe(true);
    const pasted = {input: "/btw pasted\nmultiline", key: {}};
    expect(registry.normalize(pasted)).toBe(pasted);
    expect(registry.normalize({input: "c", key: {ctrl: true}}).key.ctrl).toBe(true);
  });
  test("disposal and duplicate IDs cannot leave ghost listeners", () => {
    const router = new InputRouter();
    const scope = {id: "modal", priority: 10, enabled: () => true, handle: () => true};
    const dispose = router.register(scope);
    expect(() => router.register(scope)).toThrow("DUPLICATE");
    dispose();
    expect(router.dispatch({input: "", key: {return: true}})).toBeNull();
  });
});

test("footer contains active and user-blocked B while human labels hide UUIDs", () => {
  const states = ["running", "waiting", "awaiting_user", "completed", "failed", "timed_out", "cancelling", "cancelled"];
  const workers = Object.fromEntries(states.map(status => [status, {process_id: status, parent_id: "a", actor_kind: "worker", status, created_at: status}]));
  workers.a = {...workers.running, parent_id: "", actor_kind: "supervisor"};
  expect(runningWorkers(workers as any).map(p => p.status).sort()).toEqual(["awaiting_user", "running", "waiting"]);
  expect(agentLabel({display_name: "HTML 页面", process_id: "uuid-hidden"} as any)).toBe("HTML 页面");
});
