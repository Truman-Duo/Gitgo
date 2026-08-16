// src/input/commandInput.ts — shared COMMAND-mode key resolution + action applier.
// ConfigPanel tabs (and future overlays) share the same /-command input model:
// a pure resolver turns a key event into CommandAction(s); the applier mutates
// the shared cmdInput buffer + suggestion index. No React, no side effects here.

import type { Dispatch, SetStateAction } from "react";
import type { TextOp } from "./keymap.js";
import { applyTextOp, type UseTextInputReturn } from "../hooks/useTextInput.js";
import { matchChord } from "./bindings.js";

export type CommandAction =
  | { type: "insertSlash" }
  | { type: "run" }
  | { type: "suggestionUp" }
  | { type: "suggestionDown" }
  | { type: "suggestionTab" }
  | { type: "text"; op: TextOp };

export function isCommandMode(cmdValue: string, input: string): boolean {
  return cmdValue.startsWith("/") || (input === "/" && cmdValue === "");
}

export function resolveCommandKeys(
  cmdValue: string,
  suggestionCount: number,
  input: string,
  key: any,
): CommandAction[] {
  if (input === "/" && cmdValue === "") return [{ type: "insertSlash" }];
  if (matchChord("enter", input, key)) return [{ type: "run" }];
  if (matchChord("up", input, key) && suggestionCount > 0) return [{ type: "suggestionUp" }];
  if (matchChord("down", input, key) && suggestionCount > 0) return [{ type: "suggestionDown" }];
  if (matchChord("tabAny", input, key) && suggestionCount > 0) return [{ type: "suggestionTab" }];
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

export type CommandHandlers = {
  cmdInput: UseTextInputReturn;
  suggestionLabels: string[];
  suggestionIdx: number;
  setSuggestionIdx: Dispatch<SetStateAction<number>>;
  runCommand: (cmd: string) => void;
};

export function applyCommandAction(a: CommandAction, h: CommandHandlers): void {
  switch (a.type) {
    case "insertSlash":
      h.cmdInput.insert("/");
      break;
    case "run": {
      const cmd = h.cmdInput.value.trim();
      if (cmd) {
        h.runCommand(cmd);
        h.setSuggestionIdx(0);
      }
      break;
    }
    case "suggestionUp":
      h.setSuggestionIdx((s) => (s - 1 + h.suggestionLabels.length) % h.suggestionLabels.length);
      break;
    case "suggestionDown":
      h.setSuggestionIdx((s) => (s + 1) % h.suggestionLabels.length);
      break;
    case "suggestionTab":
      h.cmdInput.setValue(h.suggestionLabels[h.suggestionIdx % h.suggestionLabels.length]);
      break;
    case "text":
      applyTextOp(a.op, h.cmdInput);
      break;
  }
}
