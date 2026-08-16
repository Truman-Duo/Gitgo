// src/input/overlays/quit.ts — QuitPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc cancel, ↑↓ move, Enter confirm
export function resolveQuitKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}
