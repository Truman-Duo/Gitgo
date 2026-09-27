// src/input/overlays/help.ts — HelpPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Help is modal: only the canonical cancel binding dismisses it.
export function resolveHelpKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  return [];
}
