import { describe, expect, test } from "bun:test";
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
  test("Tab reaches running B even during a pending decision, without submitting it", () => {
    const ctx = context({decisionOptionLabels: ["Continue", "Stop"], runningBIds: ["b"], chatBusy: true});
    expect(resolveSceneKey(ctx, "", {tab: true})).toEqual([
      {kind: "state", action: {type: "set_status_bar_focused", focused: true}},
    ]);
    expect(resolveSceneKey({...ctx, statusBarFocused: true}, "", {return: true})).toEqual([
      {kind: "state", action: {type: "navigate", scene: "agent_detail", patch: {activeAgentId: "b"}}},
    ]);
    expect(resolveSceneKey({...ctx, mode: "COMMAND", cmdValue: "b"}, "", {tab: true}))
      .not.toContainEqual({kind: "state", action: {type: "set_status_bar_focused", focused: true}});
  });
  test("selects and submits an option without model-side UI parsing", () => {
    const ctx = context({
      decisionOptionLabels: ["Keep API", "Break API"],
      decisionAllowFreeForm: true,
      decisionSelection: 0,
    });

    expect(resolveSceneKey(ctx, "", { downArrow: true })).toEqual([
      { kind: "state", action: { type: "set_decision_selection", index: 1 } },
    ]);
    expect(resolveSceneKey({ ...ctx, decisionSelection: 1 }, "", { return: true })).toEqual([
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
    expect(resolveSceneKey(ctx, "", {return: true})).toContainEqual({
      kind: "effect",
      effect: {type: "send_chat", text: "Choose option 2: Detailed", decisionSubmission: true},
    });
  });

  test("Ctrl+G opens the external prompt editor in both A and B chat scenes", () => {
    for (const scene of ["workspace", "agent_detail"] as const) {
      expect(resolveSceneKey(context({scene}), "g", {ctrl: true})).toEqual([
        {kind: "effect", effect: {type: "edit_prompt_external"}},
      ]);
    }
  });

  test("up and down move within a focused wrapped prompt instead of scrolling chat", () => {
    const ctx = context({chatInputFocused: true, textValue: "a".repeat(160), textCursor: 90});
    expect(resolveSceneKey(ctx, "", {upArrow: true})).toEqual([
      {kind: "text", buffer: "text", op: {op: "move_visual_line", delta: -1, width: 76}},
    ]);
    expect(resolveSceneKey(ctx, "", {downArrow: true})).toEqual([
      {kind: "text", buffer: "text", op: {op: "move_visual_line", delta: 1, width: 76}},
    ]);
  });

  test("offers amend and discuss shortcuts only for free-form decisions", () => {
    const ctx = context({
      decisionOptionLabels: ["Keep API"],
      decisionAllowFreeForm: true,
    });
    const amend = resolveSceneKey(ctx, "a", {});
    expect(amend).toContainEqual({
      kind: "state",
      action: { type: "set_decision_composing", composing: true },
    });
    expect(amend).toContainEqual({
      kind: "text",
      buffer: "text",
      op: { op: "set_value", text: "Choose option 1: Keep API; conditions: " },
    });

    const strict = resolveSceneKey({ ...ctx, decisionAllowFreeForm: false }, "d", {});
    expect(strict).toEqual([]);
  });
});

describe("list-first scenes", () => {
  test("ignore stray text and open the command editor only with slash", () => {
    const ctx = context({ scene: "projects", mode: "COMMAND", activeProject: null });
    expect(resolveSceneKey(ctx, "q", {})).toEqual([]);
    expect(resolveSceneKey(ctx, "/", {})).toEqual([
      { kind: "text", buffer: "cmd", op: { op: "insert", text: "/" } },
    ]);
  });
});

describe("single in-flight chat", () => {
  test("keeps the draft and reports busy instead of launching a competing stream", () => {
    const ctx = context({ textValue: "next message", textCursor: 12, chatBusy: true });
    expect(resolveSceneKey(ctx, "", { return: true })).toEqual([
      { kind: "effect", effect: { type: "report_notice", code: 2004 } },
    ]);
  });

  test("a bound decision remains submittable while the root stream is live", () => {
    const ctx = context({
      chatBusy: true,
      decisionOptionLabels: ["Allow once", "Deny"],
      decisionSelection: 0,
    });
    expect(resolveSceneKey(ctx, "", { return: true })).toEqual([
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
    expect(resolveSceneKey(ctx, "", { escape: true })).toEqual([
      { kind: "state", action: { type: "push_overlay", overlay: "interruptConfirm", props: { processId: "root-live", project: "gitgo", subtree: true } } },
    ]);
  });

  test("Escape can cancel an accepted request before daemon admission assigns a process", () => {
    const ctx = context({
      chatBusy: true,
      chatInputFocused: true,
      interruptTarget: { running: true, requestId: "dashboard-request-1" },
    });
    expect(resolveSceneKey(ctx, "", { escape: true })).toEqual([
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
    expect(resolveSceneKey(ctx, "", { escape: true })).toEqual([
      { kind: "state", action: { type: "push_overlay", overlay: "interruptConfirm", props: { processId: "child-live", project: "gitgo", subtree: false } } },
    ]);
    expect(resolveSceneKey({ ...ctx, chatInputFocused: false }, "", { leftArrow: true })).toEqual([
      { kind: "text", buffer: "text", op: { op: "set_value", text: "" } },
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
    expect(resolveCommandInput(ctx, "", { return: true })).toEqual([
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
    expect(resolveCommandInput(ctx, "", { tab: true })).toContainEqual({
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "btw " },
    });
  });

  test("Tab completes every chat command without duplicating the rendered slash", () => {
    const ctx = context({
      mode: "COMMAND",
      cmdValue: "co",
      cmdCursor: 2,
      suggestions: [{ label: "/compact", description: "Compact context" }],
    });
    expect(resolveCommandInput(ctx, "", { tab: true })).toContainEqual({
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "compact" },
    });
  });

  test("Tab preserves the slash when the command bar itself owns it", () => {
    const ctx = context({
      scene: "projects",
      mode: "COMMAND",
      cmdValue: "/st",
      cmdCursor: 3,
      suggestions: [{ label: "/stats", description: "Usage overview" }],
    });
    expect(resolveCommandInput(ctx, "", { tab: true })).toContainEqual({
      kind: "text", buffer: "cmd", op: { op: "set_value", text: "/stats" },
    });
  });
});
