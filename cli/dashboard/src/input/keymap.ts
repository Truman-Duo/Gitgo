// src/input/keymap.ts — pure key→action resolution.
// Single source of truth for keyboard dispatch. Takes a read-only snapshot of
// UI state (InputContext) + a key event, returns a list of InputActions the
// thin controller applies. No React, no side effects here (except the
// clipboard paste, which is handled by the controller before this runs).
//
// Three action kinds:
//   state  → dispatched to the store reducer (state/store.ts).
//   text   → drives an imperative useTextInput buffer (cmd / text / llm).
//   effect → async/side-effecting intents (run_command / stop_process / send_chat).

import type {
  AppAction,
  NavigatePatch,
  Mode,
  OverlayType,
  Scene,
} from "../state/store.js";
import { isChatScene } from "../state/store.js";
import type { Suggestion } from "../components/CommandBar.js";
import { matchChord } from "./bindings.js";

// ── Text buffer ops (drive useTextInput methods) ──────────

export type TextBuffer = "cmd" | "text" | "llm";

export type TextOp =
  | { op: "insert"; text: string }
  | { op: "delete_back" }
  | { op: "delete_forward" }
  | { op: "move_cursor"; delta: number }
  | { op: "move_word"; delta: number }
  | { op: "move_to_start" }
  | { op: "move_to_end" }
  | { op: "kill_to_end" }
  | { op: "kill_to_start" }
  | { op: "kill_word_back" }
  | { op: "yank" }
  | { op: "set_value"; text: string };

// ── Effects (async / side-effecting intents) ──────────────

export type EffectAction =
  | { type: "run_command"; cmd: string }
  | { type: "stop_process"; pid: string }
  | { type: "send_chat"; text: string }
  | { type: "scroll_chat"; delta: number }
  | { type: "scroll_chat_bottom" }
  | { type: "report_notice"; code: number; params?: Record<string, any> };

// ── InputAction (keymap output) ───────────────────────────

export type InputAction =
  | { kind: "state"; action: AppAction }
  | { kind: "text"; buffer: TextBuffer; op: TextOp }
  | { kind: "effect"; effect: EffectAction };

// ── Context (read-only snapshot of UI state) ──────────────

export type InterruptTarget = { pid?: string; running: boolean };

export type InputContext = {
  scene: Scene;
  mode: Mode;
  chatInputFocused: boolean;

  // command buffer (cmdInput)
  cmdValue: string;
  cmdCursor: number;

  // text buffer (textInput)
  textValue: string;
  textCursor: number;

  // projects / selection
  projectsLength: number;
  projectNames: string[];
  sel: number;
  activeProject: string | null;

  // process list
  processListIds: string[];
  processListSelIdx: number;

  // running B footer strip (workspace chat)
  runningBIds: string[];
  runningBSelIdx: number;
  statusBarFocused: boolean;

  // command suggestions / history
  suggestions: Suggestion[];
  suggestionIdx: number;
  cmdHistory: string[];
  cmdHistoryIdx: number;

  // interrupt resolution (computed by controller from live process map)
  interruptTarget: InterruptTarget;
};

// ── Action builders ───────────────────────────────────────

function state(a: AppAction): InputAction {
  return { kind: "state", action: a };
}
function text(buffer: TextBuffer, op: TextOp): InputAction {
  return { kind: "text", buffer, op };
}
function effect(e: EffectAction): InputAction {
  return { kind: "effect", effect: e };
}

// navigate clears both input buffers (matches App.navigate helper)
function nav(scene: Scene, patch?: NavigatePatch): InputAction[] {
  return [
    text("text", { op: "set_value", text: "" }),
    text("cmd", { op: "set_value", text: "" }),
    state({ type: "navigate", scene, patch }),
  ];
}

function pushOverlay(overlay: OverlayType, props?: Record<string, any>): InputAction[] {
  return [
    text("cmd", { op: "set_value", text: "" }),
    state({ type: "push_overlay", overlay, props }),
  ];
}

function enterCommand(): InputAction[] {
  return [
    text("cmd", { op: "set_value", text: "" }),
    state({ type: "enter_command" }),
  ];
}

function exitCommand(alsoResetHistory = false): InputAction[] {
  const acts: InputAction[] = [state({ type: "exit_command" })];
  if (alsoResetHistory) acts.push(state({ type: "set_cmd_history_idx", index: -1 }));
  return acts;
}

// ── Scene keymap ──────────────────────────────────────────

export function resolveSceneKey(ctx: InputContext, input: string, key: any): InputAction[] {
  // P1: COMMAND mode
  if (ctx.mode === "COMMAND") {
    if (!isChatScene(ctx.scene)) {
      const cmdVal = ctx.cmdValue;
      if (!cmdVal.startsWith("/")) {
        // List navigation mode: arrows move selection, Enter enters.
        if (ctx.scene === "projects") {
          if (matchChord("up", input, key)) return [state({ type: "select_project", index: Math.max(0, ctx.sel - 1) })];
          if (matchChord("down", input, key)) return [state({ type: "select_project", index: Math.min(ctx.projectsLength - 1, ctx.sel + 1) })];
          if (matchChord("enter", input, key) && ctx.projectsLength > 0) {
            return nav("workspace", { activeProject: ctx.projectNames[ctx.sel] ?? null, activeAgentId: null });
          }
        } else if (ctx.scene === "process_list") {
          if (matchChord("escape", input, key)) return nav("workspace");
          if (matchChord("tab", input, key)) return nav("workspace");
          if (matchChord("up", input, key)) return [state({ type: "select_process", index: Math.max(0, ctx.processListSelIdx - 1) })];
          if (matchChord("down", input, key)) return [state({ type: "select_process", index: Math.min(ctx.processListIds.length - 1, ctx.processListSelIdx + 1) })];
          if (matchChord("enter", input, key)) {
            const pid = ctx.processListIds[ctx.processListSelIdx];
            if (pid) return [state({ type: "navigate", scene: "agent_detail", patch: { activeAgentId: pid } })];
            return [];
          }
        }
        if (matchChord("escape", input, key)) return [];
      }
    }
    return resolveCommandInput(ctx, input, key);
  }

  // P1.5: Running B strip selection mode (workspace, statusBarFocused) — modal keys
  if (ctx.scene === "workspace" && ctx.statusBarFocused) {
    const len = ctx.runningBIds.length;
    if (len === 0) return [state({ type: "set_status_bar_focused", focused: false })];
    if (matchChord("left", input, key)) return [state({ type: "select_running_b", index: (ctx.runningBSelIdx - 1 + len) % len })];
    if (matchChord("right", input, key)) return [state({ type: "select_running_b", index: (ctx.runningBSelIdx + 1) % len })];
    if (matchChord("enterNoShift", input, key)) {
      const pid = ctx.runningBIds[ctx.runningBSelIdx % len];
      if (pid) return [state({ type: "navigate", scene: "agent_detail", patch: { activeAgentId: pid } })];
      return [];
    }
    if (matchChord("tab", input, key)) return [state({ type: "set_status_bar_focused", focused: false })];
    if (matchChord("escape", input, key)) return [state({ type: "set_status_bar_focused", focused: false })];
  }

  // P2: Escape — unfocus, agent_detail exits to workspace (A), else interrupt (chat scenes only)
  if (matchChord("escape", input, key)) {
    if (ctx.chatInputFocused) return [state({ type: "set_chat_input_focused", focused: false })];
    if (ctx.scene === "agent_detail") return nav("workspace", { activeAgentId: null });
    if (!isChatScene(ctx.scene)) return [];
    const t = ctx.interruptTarget;
    if (t.pid && t.running) return [effect({ type: "stop_process", pid: t.pid })];
    return [effect({ type: "report_notice", code: 2001 })];
  }

  // P3: "/" enters COMMAND mode (empty text only)
  if (matchChord("slash", input, key) && ctx.textValue === "") {
    return enterCommand();
  }

  // P4: Global shortcuts
  if (matchChord("question", input, key)) {
    return pushOverlay("whichkey");
  }

  // P5: Screen-specific navigation
  if (ctx.scene === "workspace") {
    if (matchChord("tab", input, key)) {
      if (ctx.runningBIds.length > 0) return [state({ type: "set_status_bar_focused", focused: !ctx.statusBarFocused })];
      return [];
    }
    if (matchChord("shiftEnter", input, key)) return [state({ type: "set_chat_input_focused", focused: true })];
  }
  if (ctx.scene === "agent_detail") {
    if (matchChord("shiftTab", input, key)) return [state({ type: "set_chat_input_focused", focused: !ctx.chatInputFocused })];
  }

  // P5.5: Chat scroll (unfocused only — typing keeps the cursor keys)
  if (isChatScene(ctx.scene) && !ctx.chatInputFocused && !ctx.statusBarFocused) {
    if (matchChord("up", input, key)) return [effect({ type: "scroll_chat", delta: -1 })];
    if (matchChord("down", input, key)) return [effect({ type: "scroll_chat", delta: 1 })];
    if (matchChord("pageUp", input, key)) return [effect({ type: "scroll_chat", delta: -10 })];
    if (matchChord("pageDown", input, key)) return [effect({ type: "scroll_chat", delta: 10 })];
    if (matchChord("end", input, key)) return [effect({ type: "scroll_chat_bottom" })];
  }

  // P5.6: Left arrow — cursor when typing, back navigation when idle
  if (matchChord("left", input, key)) {
    if (ctx.chatInputFocused) {
      if (ctx.textCursor === 0) return [state({ type: "set_chat_input_focused", focused: false })];
      return [text("text", { op: "move_cursor", delta: -1 })];
    }
    if (ctx.scene === "agent_detail") return nav("workspace", { activeAgentId: null });
    if (ctx.scene === "workspace") return nav("projects");
    return [];
  }
  if (matchChord("right", input, key)) return [text("text", { op: "move_cursor", delta: 1 })];
  if (matchChord("home", input, key)) return [text("text", { op: "move_to_start" })];
  if (matchChord("end", input, key)) return [text("text", { op: "move_to_end" })];
  if (matchChord("delete", input, key)) return [text("text", { op: "delete_forward" })];

  // P5.7: Emacs word nav (Ctrl+Left/Right, Alt+B/F)
  if (matchChord("ctrlLeft", input, key)) return [text("text", { op: "move_word", delta: -1 })];
  if (matchChord("ctrlRight", input, key)) return [text("text", { op: "move_word", delta: 1 })];
  if (matchChord("altB", input, key)) return [text("text", { op: "move_word", delta: -1 })];
  if (matchChord("altF", input, key)) return [text("text", { op: "move_word", delta: 1 })];

  // P5.8: Kill-ring (Ctrl+K/U/W/Y)
  if (matchChord("ctrlK", input, key)) return [text("text", { op: "kill_to_end" })];
  if (matchChord("ctrlU", input, key)) return [text("text", { op: "kill_to_start" })];
  if (matchChord("ctrlW", input, key)) return [text("text", { op: "kill_word_back" })];
  if (matchChord("ctrlY", input, key)) return [text("text", { op: "yank" })];

  // P6: NORMAL text input — insert + auto-focus
  if (input && input.length >= 1 && !key.ctrl && !key.meta) {
    const acts: InputAction[] = [text("text", { op: "insert", text: input })];
    if (!ctx.chatInputFocused && (ctx.scene === "projects" || ctx.scene === "workspace" || ctx.scene === "agent_detail")) {
      acts.push(state({ type: "set_chat_input_focused", focused: true }));
    }
    return acts;
  }

  // P7: Backspace in NORMAL
  if (matchChord("backspace", input, key) && ctx.textValue.length > 0 && ctx.textCursor > 0) {
    return [text("text", { op: "delete_back" })];
  }

  // P8: Enter in NORMAL
  if (matchChord("enter", input, key)) {
    if (ctx.textValue.trim()) {
      const txt = ctx.textValue.trim();
      const acts: InputAction[] = [text("text", { op: "set_value", text: "" })];
      if (isChatScene(ctx.scene)) acts.push(effect({ type: "send_chat", text: txt }));
      return acts;
    }
    if (ctx.scene === "projects" && ctx.projectsLength > 0) {
      return nav("workspace", { activeProject: ctx.projectNames[ctx.sel] ?? null, activeAgentId: null });
    }
    return [];
  }

  return [];
}

// ── Command input keymap (COMMAND-mode editing) ───────────

export function resolveCommandInput(ctx: InputContext, input: string, key: any): InputAction[] {
  const canExitCommand = isChatScene(ctx.scene);
  const cmdBuf = ctx.cmdValue;
  const cmdCursor = ctx.cmdCursor;
  const suggestions = ctx.suggestions;
  const suggestionIdx = ctx.suggestionIdx;
  const suggestionsActive =
    suggestions.length > 0 && (isChatScene(ctx.scene) ? true : cmdBuf.startsWith("/"));

  if (matchChord("enter", input, key)) {
    if (suggestionsActive) {
      const s = suggestions[suggestionIdx % suggestions.length];
      if (s) {
        let fullCmd: string;
        if (s.label.startsWith("/")) {
          fullCmd = s.label;
        } else {
          const lastSpace = cmdBuf.lastIndexOf(" ");
          fullCmd = lastSpace >= 0 ? cmdBuf.slice(0, lastSpace + 1) + s.label : s.label;
        }
        return [effect({ type: "run_command", cmd: fullCmd })];
      }
    }
    const cmd = cmdBuf.trim();
    if (cmd) return [effect({ type: "run_command", cmd })];
    if (canExitCommand) {
      return [text("cmd", { op: "set_value", text: "" }), ...exitCommand()];
    }
    return [];
  }

  if (matchChord("escape", input, key)) {
    if (cmdBuf.length > 0) {
      return [
        text("cmd", { op: "set_value", text: "" }),
        state({ type: "set_cmd_history_idx", index: -1 }),
      ];
    }
    if (canExitCommand) return exitCommand(true);
    return [];
  }

  if (matchChord("left", input, key)) return [text("cmd", { op: "move_cursor", delta: -1 })];
  if (matchChord("right", input, key)) return [text("cmd", { op: "move_cursor", delta: 1 })];
  if (matchChord("home", input, key)) return [text("cmd", { op: "move_to_start" })];
  if (matchChord("end", input, key)) return [text("cmd", { op: "move_to_end" })];

  if (matchChord("backspace", input, key)) {
    if (cmdCursor > 0) return [text("cmd", { op: "delete_back" })];
    if (canExitCommand) return exitCommand();
    return [];
  }
  if (matchChord("delete", input, key)) {
    if (cmdCursor < cmdBuf.length) return [text("cmd", { op: "delete_forward" })];
    return [];
  }

  if (matchChord("up", input, key)) {
    if (suggestionsActive) {
      return [state({ type: "set_suggestion_idx", index: (suggestionIdx - 1 + suggestions.length) % suggestions.length })];
    }
    const cmdHistory = ctx.cmdHistory;
    const cmdHistoryIdx = ctx.cmdHistoryIdx;
    if (cmdHistory.length > 0) {
      const nextIdx = cmdHistoryIdx === -1 ? cmdHistory.length - 1 : Math.max(0, cmdHistoryIdx - 1);
      return [
        text("cmd", { op: "set_value", text: cmdHistory[nextIdx] }),
        state({ type: "set_cmd_history_idx", index: nextIdx }),
      ];
    }
    if (canExitCommand) {
      return [text("cmd", { op: "set_value", text: "" }), ...exitCommand()];
    }
    return [];
  }

  if (matchChord("down", input, key)) {
    if (suggestionsActive) {
      return [state({ type: "set_suggestion_idx", index: (suggestionIdx + 1) % suggestions.length })];
    }
    const cmdHistoryIdx = ctx.cmdHistoryIdx;
    if (cmdHistoryIdx > -1) {
      const nextIdx = cmdHistoryIdx + 1;
      if (nextIdx >= ctx.cmdHistory.length) {
        return [
          text("cmd", { op: "set_value", text: "" }),
          state({ type: "set_cmd_history_idx", index: -1 }),
        ];
      }
      return [
        text("cmd", { op: "set_value", text: ctx.cmdHistory[nextIdx] }),
        state({ type: "set_cmd_history_idx", index: nextIdx }),
      ];
    }
    return [];
  }

  if (matchChord("tabAny", input, key) && suggestionsActive) {
    const s = suggestions[suggestionIdx % suggestions.length];
    if (s) {
      let value: string;
      if (s.label.startsWith("/")) {
        value = s.label;
      } else {
        const lastSpace = cmdBuf.lastIndexOf(" ");
        value = lastSpace >= 0 ? cmdBuf.slice(0, lastSpace + 1) + s.label : s.label;
      }
      return [
        text("cmd", { op: "set_value", text: value }),
        state({ type: "set_suggestion_idx", index: suggestionIdx + 1 }),
      ];
    }
    return [];
  }

  if (input && input.length >= 1 && !key.ctrl && !key.meta) {
    return [text("cmd", { op: "insert", text: input })];
  }

  return [];
}
