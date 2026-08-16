// src/input/overlays/status.ts — StatusPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc dismiss, ↑↓ move, Enter confirm
export function resolveStatusKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}
