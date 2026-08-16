// src/input/overlays/runtimeMenu.ts — RuntimeMenu key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc dismiss, ←→ switch tab, Enter confirm
export function resolveRuntimeMenuKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("left", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("right", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}
