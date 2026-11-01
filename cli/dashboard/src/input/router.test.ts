import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { InputRouter, InputBindingRegistry, presetSettings } from "./router.js";
import { runningWorkers, agentLabel } from "../daemon/agentLabels.js";

describe("central input ownership", () => {
  test("mode and BTW rebinding preserve form navigation and remove displaced defaults", () => {
    const registry = new InputBindingRegistry();
    const previous = {input: "", key: {tab: true, shift: true}};
    assert.equal(registry.normalize(previous, "task").key.bindingAction, "advancement.toggle");
    assert.equal(registry.normalize(previous, "form").key.bindingAction, "navigation.previousTab");
    assert.equal(registry.normalize(previous, "btw").key.bindingAction, undefined);
    assert.equal(registry.normalize({input:"m",key:{meta:true}}, "btw").key.bindingAction, "btw.previousRegion");
    registry.configure({version:1,overrides:{"advancement.toggle":{key:"m",ctrl:true},"btw.previousRegion":{key:"p",meta:true}}});
    assert.equal(registry.normalize({input:"m",key:{ctrl:true}}, "task").key.bindingAction, "advancement.toggle");
    assert.equal(registry.normalize(previous, "task").key.bindingAction, undefined);
    assert.equal(registry.normalize(previous, "form").key.bindingAction, "navigation.previousTab");
    assert.equal(registry.normalize({input:"m",key:{meta:true}}, "btw").key.bindingAction, undefined);
  });
  test("portable preset is reversible and does not mistake Super for Ctrl", () => {
    const registry = new InputBindingRegistry();
    registry.configure(presetSettings("portable"));
    assert.equal(registry.label("editor.newline"), "Alt+Enter");
    assert.equal(registry.normalize({input:"",key:{return:true,meta:true}}).input, "\n");
    assert.equal(registry.normalize({input:"p",key:{ctrl:true}}, "form").key.tab, true);
    const superP = {input:"p",key:{super:true}};
    assert.deepEqual(registry.normalize(superP), superP);
    registry.configure(presetSettings("standard"));
    assert.equal(registry.label("editor.newline"), "Shift+Enter");
    assert.equal(registry.normalize({input:"",key:{return:true,shift:true}}).input, "\n");
  });
  test("overlay scroll owns wheel and paging while other keys reach one modal", () => {
    const router = new InputRouter();
    const calls: string[] = [];
    let overlay = true;
    router.register({ id: "scene", priority: 0, enabled: () => true, handle: () => { calls.push("scene"); return true; } });
    router.register({ id: "dialog", priority: 100, enabled: () => overlay, handle: () => { calls.push("dialog"); return true; } });
    router.register({ id: "scroll", priority: 150, enabled: () => overlay, handle: ({key}) => !!(key.wheelUp || key.pageDown) });
    assert.strictEqual(router.dispatch({input: "", key: {wheelUp: true}}), "scroll");
    assert.strictEqual(router.dispatch({input: "", key: {pageDown: true}}), "scroll");
    assert.deepStrictEqual(calls, []);
    assert.strictEqual(router.dispatch({input: "", key: {tab: true}}), "dialog");
    overlay = false;
    assert.strictEqual(router.dispatch({input: "", key: {tab: true}}), "scene");
    assert.deepStrictEqual(calls, ["dialog", "scene"]);
  });
  test("rebinds atomically, rejects conflicts, removes displaced defaults", () => {
    const registry = new InputBindingRegistry();
    registry.configure({version: 1, overrides: {"execution.interrupt": {key: "x", ctrl: true}}});
    assert.strictEqual(registry.normalize({input: "x", key: {ctrl: true}}).key.escape, true);
    assert.strictEqual(registry.normalize({input: "", key: {escape: true, meta: true}}).key.escape, undefined);
    assert.throws(() => registry.configure({version: 1, overrides: {"execution.interrupt": {key: "c", ctrl: true}}}), new RegExp("CONFLICT"));
    assert.deepStrictEqual(registry.snapshot().overrides["execution.interrupt"], {key: "x", ctrl: true});
  });
  test("recognizes Ink Escape metadata without rewriting paste to actions", () => {
    const registry = new InputBindingRegistry();
    assert.strictEqual(registry.normalize({input: "", key: {escape: true, meta: true}}).key.escape, true);
    const pasted = {input: "/btw pasted\nmultiline", key: {}};
    assert.strictEqual(registry.normalize(pasted), pasted);
    assert.strictEqual(registry.normalize({input: "c", key: {ctrl: true}}).key.ctrl, true);
  });
  test("disposal and duplicate IDs cannot leave ghost listeners", () => {
    const router = new InputRouter();
    const scope = {id: "modal", priority: 10, enabled: () => true, handle: () => true};
    const dispose = router.register(scope);
    assert.throws(() => router.register(scope), new RegExp("DUPLICATE"));
    dispose();
    assert.strictEqual(router.dispatch({input: "", key: {return: true}}), null);
  });
});

test("footer contains active and user-blocked B while human labels hide UUIDs", () => {
  const states = ["running", "waiting", "awaiting_user", "completed", "failed", "timed_out", "cancelling", "cancelled"];
  const workers = Object.fromEntries(states.map(status => [status, {process_id: status, parent_id: "a", actor_kind: "worker", status, created_at: status}]));
  workers.a = {...workers.running, parent_id: "", actor_kind: "supervisor"};
  assert.deepStrictEqual(runningWorkers(workers as any).map(p => p.status).sort(), ["awaiting_user", "running", "waiting"]);
  assert.strictEqual(agentLabel({display_name: "HTML 页面", process_id: "uuid-hidden"} as any), "HTML 页面");
});

test("scoped remapping does not steal typing or require unrelated contexts to use unique keys",()=>{
 const registry=new InputBindingRegistry();
 registry.configure({version:1,overrides:{"trial.accept":{key:"j",ctrl:true},"formal.delete":{key:"j",ctrl:true}}});
 assert.equal(registry.normalize({input:"j",key:{ctrl:true}},"trial").input,"a");
 assert.equal(registry.normalize({input:"j",key:{ctrl:true}},"formal").input,"d");
 assert.deepEqual(registry.normalize({input:"a",key:{}},"scene"),{input:"a",key:{}});
 assert.deepEqual(registry.normalize({input:"a",key:{}},"trial"),{input:"",key:{}});
 assert.throws(()=>registry.configure({version:1,overrides:{"trial.accept":{key:"j",ctrl:true},"trial.discard":{key:"j",ctrl:true}}}),/CONFLICT/);
 assert.equal(registry.label("trial.accept"),"Ctrl+J");
 assert.throws(()=>registry.configure({version:1,overrides:{"editor.external":{key:"g"}}}),/TEXT_CONFLICT/);
});

test("original uppercase letter aliases survive until their action is rebound",()=>{
 const registry=new InputBindingRegistry();
 assert.equal(registry.normalize({input:"A",key:{shift:true}},"trial").key.bindingAction,"trial.accept");
 assert.throws(()=>registry.configure({version:1,overrides:{"trial.discard":{key:"a",shift:true}}}),/CONFLICT/);
 registry.configure({version:1,overrides:{"trial.accept":{key:"j",ctrl:true}}});
 assert.deepEqual(registry.normalize({input:"A",key:{shift:true}},"trial"),{input:"",key:{}});
 assert.deepEqual(registry.normalize({input:"A",key:{shift:true}},"scene"),{input:"A",key:{shift:true}});
});
