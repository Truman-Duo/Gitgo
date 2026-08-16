// src/input/overlays/createProject.ts — CreateProjectPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { TextOp } from "../keymap.js";

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
