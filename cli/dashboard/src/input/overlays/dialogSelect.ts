// src/input/overlays/dialogSelect.ts — DialogSelect key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc dismiss, Enter confirm, ↑↓ move, text editing, insert
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
