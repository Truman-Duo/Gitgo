// src/input/overlays/inlineContext.ts — InlineContext/GovernancePanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc dismiss, ←→ move tab
export function resolveInlineContextKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("left", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("right", input, key)) return [{ type: "move", delta: 1 }];
  return [];
}
