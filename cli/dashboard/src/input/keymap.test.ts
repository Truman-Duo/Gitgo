import { describe, test } from "node:test";
import assert from "node:assert/strict";
import {isDeepStrictEqual} from "node:util";
import type { InputContext } from "./keymap.js";
import { resolveCommandInput, resolveSceneKey } from "./keymap.js";

function context(patch: Partial<InputContext> = {}): InputContext {
  return {
    scene: "workspace",
    mode: "NORMAL",
    chatInputFocused: false,
    cmdValue: "",
    cmdCursor: 0,
    textValue: "",
    textCursor: 0,
    textVisualWidth: 76,
    projectsLength: 1,
    projectNames: ["gitgo"],
    sel: 0,
    activeProject: "gitgo",
    processListIds: [],
    processListSelIdx: 0,
    runningBIds: [],
    runningBSelIdx: 0,
    statusBarFocused: false,
    decisionOptionLabels: [],
    decisionAllowFreeForm: false,
    decisionSelection: 0,
    decisionComposing: false,
    decisionSubmitting: false,
    chatBusy: false,
    suggestions: [],
    suggestionIdx: 0,
    cmdHistory: [],
    cmdHistoryIdx: -1,
    interruptTarget: { running: false },
    ...patch,
  };
}

describe("host-driven decision interaction", () => {
  test("free-form replies use the decision channel while a root stream is still busy", () => {
    const ctx = context({decisionAllowFreeForm: true, chatBusy: true,
      textValue: "For my team", textCursor: 11, chatInputFocused: true});
    assert.ok(resolveSceneKey(ctx, "", {return: true}).some(item => isDeepStrictEqual(item,
      {kind: "effect", effect: {type: "send_chat", text: "For my team", decisionSubmission: true}})));
    assert.deepStrictEqual(resolveSceneKey({...ctx, decisionSubmitting: true}, "", {return: true}),
      [{kind: "effect", effect: {type: "report_notice", code: 2004}}]);
    assert.deepStrictEqual(resolveSceneKey({...ctx, decisionAllowFreeForm: false}, "", {return: true}),
      [{kind: "effect", effect: {type: "report_notice", code: 2004}}]);
  });
  test("Tab reaches running B even during a pending decision, without submitting it", () => {
    const ctx = context({decisionOptionLabels: ["Continue", "Stop"], runningBIds: ["b"], chatBusy: true});
    assert.deepStrictEqual(resolveSceneKey(ctx, "", {tab: true}), [
      {kind: "state", action: {type: "set_status_bar_focused", focused: true}},
    ]);
    assert.deepStrictEqual(resolveSceneKey({...ctx, statusBarFocused: true}, "", {return: true}), [
      {kind: "state", action: {type: "navigate", scene: "agent_detail", patch: {activeAgentId: "b"}}},
    ]);
    assert.ok(!resolveSceneKey({...ctx, mode: "COMMAND", cmdValue: "b"}, "", {tab: true})
      .some(item => isDeepStrictEqual(item, {kind: "state", action: {type: "set_status_bar_focused", focused: true}})));
  });
  test("selects and submits an option without model-side UI parsing", () => {
    const ctx = context({
      decisionOptionLabels: ["Keep API", "Break API"],
      decisionAllowFreeForm: true,
      decisionSelection: 0,
    });

    assert.deepStrictEqual(resolveSceneKey(ctx, "", { downArrow: true }), [
      { kind: "state", action: { type: "set_decision_selection", index: 1 } },
    ]);
    assert.deepStrictEqual(resolveSceneKey({ ...ctx, decisionSelection: 1 }, "", { return: true }), [
      { kind: "state", action: { type: "set_decision_submitting", submitting: true } },
      {
        kind: "effect",
        effect: {
          type: "send_chat",
          text: "Choose option 2: Break API",
          decisionSubmission: true,
        },
      },
    ]);
  });

  test("the same decision controller answers a B question in agent detail", () => {
    const ctx = context({
      scene: "agent_detail",
      decisionOptionLabels: ["Minimal", "Detailed"],
      decisionSelection: 1,
    });
    assert.ok((resolveSceneKey(ctx, "", {return: true})).some(item => isDeepStrictEqual(item, {
      kind: "effect",
      effect: {type: "send_chat", text: "Choose option 2: Detailed", decisionSubmission: true},
    })));
  });

  test("Ctrl+G opens the external prompt editor in both A and B chat scenes", () => {
    for (const scene of ["workspace", "agent_detail"] as const) {
      assert.deepStrictEqual(resolveSceneKey(context({scene}), "g", {ctrl: true}), [
        {kind: "effect", effect: {type: "edit_prompt_external"}},
      ]);
    }
  });

  test("up and down move within a focused wrapped prompt instead of scrolling chat", () => {
    const ctx = context({chatInputFocused: true, textValue: "a".repeat(160), textCursor: 90});
    assert.deepStrictEqual(resolveSceneKey(ctx, "", {upArrow: true}), [
      {kind: "text", buffer: "text", op: {op: "move_visual_line", delta: -1, width: 76}},
    ]);
    assert.deepStrictEqual(resolveSceneKey(ctx, "", {downArrow: true}), [
      {kind: "text", buffer: "text", op: {op: "move_visual_line", delta: 1, width: 76}},
    ]);
  });

  test("offers amend and discuss shortcuts only for free-form decisions", () => {
    const ctx = context({
      decisionOptionLabels: ["Keep API"],
      decisionAllowFreeForm: true,
    });
    const amend = resolveSceneKey(ctx, "a", {});
    assert.ok((amend).some(item => isDeepStrictEqual(item, {
      kind: "state",
      action: { type: "set_decision_composing", composing: true },
    })));
    assert.ok((amend).some(item => isDeepStrictEqual(item, {
      kind: "text",
      buffer: "text",
      op: { op: "set_value", text: "Choose option 1: Keep API; conditions: " },
    })));

    const strict = resolveSceneKey({ ...ctx, decisionAllowFreeForm: false }, "d", {});
    assert.deepStrictEqual(strict, []);
  });
});

describe("list-first scenes", () => {
  test("ignore stray text and open the command editor only with slash", () => {
    const ctx = context({ scene: "projects", mode: "COMMAND", activeProject: null });
    assert.deepStrictEqual(resolveSceneKey(ctx, "q", {}), []);
    assert.deepStrictEqual(resolveSceneKey(ctx, "/", {}), [
      { kind: "text", buffer: "cmd", op: { op: "insert", text: "/" } },
    ]);
  });
});

describe("single in-flight chat", () => {
  test("keeps the draft and reports busy instead of launching a competing stream", () => {
    const ctx = context({ textValue: "next message", textCursor: 12, chatBusy: true });
    assert.deepStrictEqual(resolveSceneKey(ctx, "", { return: true }), [
      { kind: "effect", effect: { type: "report_notice", code: 2004 } },
    ]);
  });

  test("a bound decision remains submittable while the root stream is live", () => {
    const ctx = context({
      chatBusy: true,
      decisionOptionLabels: ["Allow once", "Deny"],
      decisionSelection: 0,
    });
    assert.deepStrictEqual(resolveSceneKey(ctx, "", { return: true }), [
      { kind: "state", action: { type: "set_decision_submitting", submitting: true } },
      {
        kind: "effect",
        effect: {
          type: "send_chat",
          text: "Choose option 1: Allow once",
          decisionSubmission: true,
        },
      },
    ]);
  });

  test("Escape interrupts the acknowledged root before changing input focus", () => {
    const ctx = context({
      chatBusy: true,
      chatInputFocused: true,
      interruptTarget: { running: true, pid: "root-live" },
    });
    assert.deepStrictEqual(resolveSceneKey(ctx, "", { escape: true }), [
      { kind: "state", action: { type: "push_overlay", overlay: "interruptConfirm", props: { processId: "root-live", requestId: undefined, project: "gitgo", subtree: true } } },
    ]);
  });

  test("Escape can cancel an accepted request before daemon admission assigns a process", () => {
    const ctx = context({
      chatBusy: true,
      chatInputFocused: true,
      interruptTarget: { running: true, requestId: "dashboard-request-1" },
    });
    assert.deepStrictEqual(resolveSceneKey(ctx, "", { escape: true }), [
      { kind: "state", action: { type: "push_overlay", overlay: "interruptConfirm", props: {
        processId: undefined, requestId: "dashboard-request-1", project: "gitgo", subtree: true,
      } } },
    ]);
  });

  test("Escape in B detail stops B while left arrow alone navigates back to A", () => {
    const ctx = context({
      scene: "agent_detail",
      chatInputFocused: true,
      interruptTarget: { running: true, pid: "child-live" },
    });
    assert.deepStrictEqual(resolveSceneKey(ctx, "", { escape: true }), [
      { kind: "state", action: { type: "push_overlay", overlay: "interruptConfirm", props: { processId: "child-live", requestId: undefined, project: "gitgo", subtree: false } } },
    ]);
    assert.deepStrictEqual(resolveSceneKey({ ...ctx, chatInputFocused: false }, "", { leftArrow: true }), [
      { kind: "text", buffer: "cmd", op: { op: "set_value", text: "" } },
      { kind: "state", action: { type: "navigate", scene: "workspace", patch: { activeAgentId: null } } },
    ]);
  });
});

describe("parameterised command completion", () => {
  test("Enter fills /btw for an argument instead of submitting an empty call", () => {
    const ctx = context({
      mode: "COMMAND",
      cmdValue: "/b",
      cmdCursor: 2,
      suggestions: [{
        label: "/btw", description: "Isolated Side Question", inputMode: "fill",
      }],
    });
    assert.deepStrictEqual(resolveCommandInput(ctx, "", { return: true }), [
      { kind: "text", buffer: "cmd", op: { op: "set_value", text: "btw " } },
    ]);
  });

  test("Tab fills /btw without duplicating the rendered slash", () => {
    const ctx = context({
      mode: "COMMAND",
      cmdValue: "bt",
      cmdCursor: 2,
      suggestions: [{
        label: "/btw", description: "Isolated Side Question", inputMode: "fill",
      }],
    });
    assert.ok((resolveCommandInput(ctx, "", { tab: true })).some(item => isDeepStrictEqual(item, {
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "btw " },
    })));
  });

  test("Tab completes every chat command without duplicating the rendered slash", () => {
    const ctx = context({
      mode: "COMMAND",
      cmdValue: "co",
      cmdCursor: 2,
      suggestions: [{ label: "/compact", description: "Compact context" }],
    });
    assert.ok((resolveCommandInput(ctx, "", { tab: true })).some(item => isDeepStrictEqual(item, {
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "compact" },
    })));
  });

  test("Tab preserves the slash when the command bar itself owns it", () => {
    const ctx = context({
      scene: "projects",
      mode: "COMMAND",
      cmdValue: "/st",
      cmdCursor: 3,
      suggestions: [{ label: "/stats", description: "Usage overview" }],
    });
    assert.ok((resolveCommandInput(ctx, "", { tab: true })).some(item => isDeepStrictEqual(item, {
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "/stats" },
    })));
  });
});


test("B detail returns to its source list; focused arrows remain editor operations", () => {
  const ctx = context({scene: "agent_detail", chatInputFocused: false, agentReturnScene: "process_list"});
  const actions = resolveSceneKey(ctx, "", {leftArrow: true});
  assert.ok(actions.some(a => a.kind === "state" && a.action.type === "navigate" && a.action.scene === "process_list"));
  assert.equal(resolveSceneKey({...ctx, chatInputFocused: true}, "", {leftArrow: true})
    .some(a => a.kind === "state" && a.action.type === "navigate"), false);
});


test("left remains cursor editing for a nonempty draft, even at zero or after focus loss",()=>{
 for(const scene of ["workspace","agent_detail"] as const)for(const focused of [true,false]){
  const ctx=context({scene,chatInputFocused:focused,textValue:"unfinished draft",textCursor:0});
  assert.deepStrictEqual(resolveSceneKey(ctx,"",{leftArrow:true}),[
   {kind:"text",buffer:"text",op:{op:"move_cursor",delta:-1}}]);
 }
});
test("neutral question row composes no selected option and D never confirms a choice",()=>{
 const ctx=context({scene:"workspace",decisionOptionLabels:["one","two"],decisionAllowFreeForm:true,decisionSelection:2});
 const opened=resolveSceneKey(ctx,"",{return:true});
 assert.ok(opened.some(a=>a.kind==="state"&&a.action.type==="set_decision_composing"));
 assert.ok(!opened.some(a=>a.kind==="effect"));
 const discuss=resolveSceneKey({...ctx,decisionSelection:0},"d",{});
 assert.ok(discuss.some(a=>a.kind==="text"&&a.op.op==="set_value"&&a.op.text.startsWith("Discuss option 1")));
 assert.ok(!discuss.some(a=>a.kind==="effect"));
});
