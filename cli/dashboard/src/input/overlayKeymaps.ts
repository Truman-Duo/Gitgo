// src/input/overlayKeymaps.ts — pure key→action resolution for overlay panels.
// Overlay panels keep their own transient local state (selection, cursor, tab,
// export status) in the component; these pure keymaps translate a key event into
// a small OverlayAction list that the component's thin controller applies.
// No React, no side effects here.

import type { TextOp } from "./keymap.js";
import { matchChord } from "./bindings.js";

export type OverlayAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "confirm" }
  | { type: "text"; op: TextOp };

// HelpPanel: Esc / h / H dismiss
export function resolveHelpKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("letterH", input, key)) return [{ type: "dismiss" }];
  return [];
}

// QuitPanel: Esc cancel, ↑↓ move, Enter confirm
export function resolveQuitKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}

// StatusPanel: Esc dismiss, ↑↓ move, Enter confirm
export function resolveStatusKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}

// RuntimeMenu: Esc dismiss, ←→ switch tab, Enter confirm
export function resolveRuntimeMenuKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("left", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("right", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}

// ExportPanel: Esc dismiss; selection/confirm only while idle/error.
export function resolveExportKey(
  status: "idle" | "exporting" | "done" | "error",
  input: string,
  key: any,
): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (status === "exporting") return [];
  if (status === "done") return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}

// InlineContext: Esc dismiss, ←→ move tab
export function resolveInlineContextKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("left", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("right", input, key)) return [{ type: "move", delta: 1 }];
  return [];
}

// DialogSelect: Esc dismiss, Enter confirm, ↑↓ move, text editing, insert
export function resolveDialogSelectKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("backspace", input, key)) return [{ type: "text", op: { op: "delete_back" } }];
  if (matchChord("delete", input, key)) return [{ type: "text", op: { op: "delete_forward" } }];
  if (matchChord("left", input, key)) return [{ type: "text", op: { op: "move_cursor", delta: -1 } }];
  if (matchChord("right", input, key)) return [{ type: "text", op: { op: "move_cursor", delta: 1 } }];
  if (matchChord("home", input, key)) return [{ type: "text", op: { op: "move_to_start" } }];
  if (matchChord("end", input, key)) return [{ type: "text", op: { op: "move_to_end" } }];
  if (input && input.length >= 1 && !key.ctrl && !key.meta) {
    return [{ type: "text", op: { op: "insert", text: input } }];
  }
  return [];
}

// ── ConfigPanel ────────────────────────────────────────────

export type ConfigPanelCtx = {
  mode: "list" | "edit";
  tab: "providers" | "bin" | "publish";
  publishView: "menu" | "templates" | "push";
  templateEditMode: "idle" | "add" | "edit";
  binView: "menu" | "archived" | "delay";
  binConfirm: boolean;
  cmdValue: string;
  providersView: "projects" | "detail";
  focusCol: "main" | "failover";
  suggestionCount: number;
};

export type ConfigPanelAction =
  | { type: "editCancel" }
  | { type: "editNextField" }
  | { type: "editPrevField" }
  | { type: "editSave" }
  | { type: "editBackspace" }
  | { type: "editInsert"; text: string }
  | { type: "listEsc" }
  | { type: "cmdInsertSlash" }
  | { type: "cmdRun" }
  | { type: "cmdSuggestionUp" }
  | { type: "cmdSuggestionDown" }
  | { type: "cmdSuggestionTab" }
  | { type: "cmdText"; op: TextOp }
  | { type: "detailLeft" }
  | { type: "detailRight" }
  | { type: "detailMainMove"; delta: number }
  | { type: "detailMainConfirm" }
  | { type: "detailFoMove"; delta: number }
  | { type: "detailFoConfirm" }
  | { type: "detailOpen" }
  | { type: "tabPrev" }
  | { type: "tabNext" }
  | { type: "binDelayAdjust"; delta: number }
  | { type: "tplMove"; delta: number }
  | { type: "tplNew" }
  | { type: "tplEdit" }
  | { type: "tplDelete" }
  | { type: "tplEditSave" }
  | { type: "tplEditBackspace" }
  | { type: "tplEditInsert"; text: string }
  | { type: "tplEditCancel" }
  | { type: "tplEditSwitchField" }
  | { type: "pushMove"; delta: number }
  | { type: "pushToggle" }
  | { type: "publishMenuMove"; delta: number }
  | { type: "publishMenuConfirm" }
  | { type: "binMenuMove"; delta: number }
  | { type: "binMenuConfirm" }
  | { type: "binArchMove"; delta: number }
  | { type: "binConfirmCancel" }
  | { type: "binConfirmMove"; index: number }
  | { type: "binConfirmYes" };

export function resolveConfigKey(ctx: ConfigPanelCtx, input: string, key: any): ConfigPanelAction[] {
  // Providers edit mode
  if (ctx.mode === "edit") {
    if (matchChord("escape", input, key)) return [{ type: "editCancel" }];
    if (matchChord("tabAny", input, key)) return [{ type: "editNextField" }];
    if (key.shiftTab) return [{ type: "editPrevField" }];
    if (matchChord("enter", input, key)) return [{ type: "editSave" }];
    if (matchChord("backspace", input, key)) return [{ type: "editBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "editInsert", text: input }];
    }
    return [];
  }

  // Template edit mode (publish tab)
  if (ctx.tab === "publish" && ctx.templateEditMode !== "idle") {
    if (matchChord("escape", input, key)) return [{ type: "tplEditCancel" }];
    if (matchChord("enter", input, key)) return [{ type: "tplEditSave" }];
    if (matchChord("tabAny", input, key)) return [{ type: "tplEditSwitchField" }];
    if (matchChord("backspace", input, key) || matchChord("delete", input, key)) return [{ type: "tplEditBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "tplEditInsert", text: input }];
    }
    return [];
  }

  // Bin confirm dialog
  if (ctx.tab === "bin" && ctx.binConfirm) {
    if (matchChord("escape", input, key)) return [{ type: "binConfirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "binConfirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "binConfirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "binConfirmYes" }];
    return [];
  }

  // Esc (list mode)
  if (matchChord("escape", input, key)) return [{ type: "listEsc" }];

  // COMMAND mode
  if (ctx.cmdValue.startsWith("/") || (input === "/" && ctx.cmdValue === "")) {
    if (input === "/" && ctx.cmdValue === "") return [{ type: "cmdInsertSlash" }];
    if (matchChord("enter", input, key)) return [{ type: "cmdRun" }];
    if (matchChord("up", input, key) && ctx.suggestionCount > 0) return [{ type: "cmdSuggestionUp" }];
    if (matchChord("down", input, key) && ctx.suggestionCount > 0) return [{ type: "cmdSuggestionDown" }];
    if (matchChord("tabAny", input, key) && ctx.suggestionCount > 0) return [{ type: "cmdSuggestionTab" }];
    if (matchChord("backspace", input, key)) return [{ type: "cmdText", op: { op: "delete_back" } }];
    if (matchChord("delete", input, key)) return [{ type: "cmdText", op: { op: "delete_forward" } }];
    if (matchChord("left", input, key)) return [{ type: "cmdText", op: { op: "move_cursor", delta: -1 } }];
    if (matchChord("right", input, key)) return [{ type: "cmdText", op: { op: "move_cursor", delta: 1 } }];
    if (matchChord("home", input, key)) return [{ type: "cmdText", op: { op: "move_to_start" } }];
    if (matchChord("end", input, key)) return [{ type: "cmdText", op: { op: "move_to_end" } }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "cmdText", op: { op: "insert", text: input } }];
    }
    return [];
  }

  // Detail view (dual column, providers tab) — left/right = column switch
  if (ctx.tab === "providers" && ctx.providersView === "detail") {
    if (matchChord("left", input, key)) return [{ type: "detailLeft" }];
    if (matchChord("right", input, key)) return [{ type: "detailRight" }];
    if (ctx.focusCol === "main") {
      if (matchChord("up", input, key)) return [{ type: "detailMainMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "detailMainMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "detailMainConfirm" }];
    } else {
      if (matchChord("up", input, key)) return [{ type: "detailFoMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "detailFoMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "detailFoConfirm" }];
    }
    return [];
  }

  // Tab switching (top-level list, any tab) via left/right
  if (matchChord("left", input, key)) return [{ type: "tabPrev" }];
  if (matchChord("right", input, key)) return [{ type: "tabNext" }];

  // Per-tab content
  if (ctx.tab === "providers") {
    // providersView === "projects" here (single Default Provider row; detail handled above)
    if (matchChord("enter", input, key)) return [{ type: "detailOpen" }];
    return [];
  }

  if (ctx.tab === "bin") {
    if (ctx.binView === "menu") {
      if (matchChord("up", input, key)) return [{ type: "binMenuMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "binMenuMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "binMenuConfirm" }];
      return [];
    }
    if (ctx.binView === "delay") {
      if (matchChord("up", input, key)) return [{ type: "binDelayAdjust", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "binDelayAdjust", delta: 1 }];
      return [];
    }
    // archived
    if (matchChord("up", input, key)) return [{ type: "binArchMove", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "binArchMove", delta: 1 }];
    return [];
  }

  if (ctx.tab === "publish") {
    if (ctx.publishView === "menu") {
      if (matchChord("up", input, key)) return [{ type: "publishMenuMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "publishMenuMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "publishMenuConfirm" }];
      return [];
    }
    if (ctx.publishView === "templates") {
      if (matchChord("up", input, key)) return [{ type: "tplMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "tplMove", delta: 1 }];
      if (matchChord("letterN", input, key)) return [{ type: "tplNew" }];
      if (matchChord("letterE", input, key)) return [{ type: "tplEdit" }];
      if (matchChord("letterD", input, key)) return [{ type: "tplDelete" }];
      return [];
    }
    // push
    if (matchChord("up", input, key)) return [{ type: "pushMove", delta: -1 }];
    if (matchChord("down", input, key)) return [{ type: "pushMove", delta: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "pushToggle" }];
    return [];
  }

  return [];
}

// ── MemoryPanel ───────────────────────────────────────────

export type MemoryAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "snapshot" }
  | { type: "restore" }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveMemoryKey(confirm: boolean, input: string, key: any): MemoryAction[] {
  if (confirm) {
    if (matchChord("escape", input, key)) return [{ type: "confirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "confirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "confirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "confirmYes" }];
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterS", input, key)) return [{ type: "snapshot" }];
  if (matchChord("letterR", input, key)) return [{ type: "restore" }];
  return [];
}

// ── TrialPanel ────────────────────────────────────────────

export type TrialAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "triage"; action: string }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveTrialKey(confirm: boolean, input: string, key: any): TrialAction[] {
  if (confirm) {
    if (matchChord("escape", input, key)) return [{ type: "confirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "confirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "confirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "confirmYes" }];
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterA", input, key)) return [{ type: "triage", action: "accept" }];
  if (matchChord("letterP", input, key)) return [{ type: "triage", action: "promote" }];
  if (matchChord("letterD", input, key)) return [{ type: "triage", action: "discard" }];
  return [];
}

// ── FormalPanel ───────────────────────────────────────────

export type FormalAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "delete" }
  | { type: "dissolve" }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveFormalKey(confirm: boolean, input: string, key: any): FormalAction[] {
  if (confirm) {
    if (matchChord("escape", input, key)) return [{ type: "confirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "confirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "confirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "confirmYes" }];
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterD", input, key)) return [{ type: "delete" }];
  if (matchChord("letterX", input, key)) return [{ type: "dissolve" }];
  return [];
}

// ── LessonsPanel ──────────────────────────────────────────

export type LessonsAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "searchMode" }
  | { type: "verify" }
  | { type: "searchBack" }
  | { type: "searchRun" }
  | { type: "searchBackspace" }
  | { type: "searchInsert"; text: string };

export function resolveLessonsKey(mode: "list" | "search", input: string, key: any): LessonsAction[] {
  if (mode === "search") {
    if (matchChord("escape", input, key)) return [{ type: "searchBack" }];
    if (matchChord("enter", input, key)) return [{ type: "searchRun" }];
    if (matchChord("backspace", input, key)) return [{ type: "searchBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "searchInsert", text: input }];
    }
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterV", input, key)) return [{ type: "verify" }];
  if (matchChord("letterS", input, key)) return [{ type: "searchMode" }];
  return [];
}

// ── CreateProjectPanel ─────────────────────────────────────

export type CreateProjectAction =
  | { type: "dismiss" }
  | { type: "nextField" }
  | { type: "prevField" }
  | { type: "submit" }
  | { type: "llmPrev" }
  | { type: "llmNext" }
  | { type: "text"; op: TextOp };

export function resolveCreateProjectKey(field: string, input: string, key: any): CreateProjectAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("tab", input, key)) return [{ type: "nextField" }];
  if (matchChord("shiftTab", input, key)) return [{ type: "prevField" }];
  if (matchChord("up", input, key)) return [{ type: "prevField" }];
  if (matchChord("down", input, key)) return [{ type: "nextField" }];

  if (field === "llm") {
    if (matchChord("left", input, key)) return [{ type: "llmPrev" }];
    if (matchChord("right", input, key)) return [{ type: "llmNext" }];
    if (matchChord("enter", input, key)) return [{ type: "submit" }];
    return [];
  }

  if (matchChord("enter", input, key)) return [{ type: "submit" }];
  if (matchChord("backspace", input, key)) return [{ type: "text", op: { op: "delete_back" } }];
  if (matchChord("delete", input, key)) return [{ type: "text", op: { op: "delete_forward" } }];
  if (matchChord("left", input, key)) return [{ type: "text", op: { op: "move_cursor", delta: -1 } }];
  if (matchChord("right", input, key)) return [{ type: "text", op: { op: "move_cursor", delta: 1 } }];
  if (matchChord("home", input, key)) return [{ type: "text", op: { op: "move_to_start" } }];
  if (matchChord("end", input, key)) return [{ type: "text", op: { op: "move_to_end" } }];
  if (input && input.length >= 1 && !key.ctrl && !key.meta) {
    return [{ type: "text", op: { op: "insert", text: input } }];
  }
  return [];
}
