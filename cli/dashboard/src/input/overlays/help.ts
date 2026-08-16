// src/input/overlays/help.ts — HelpPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc / h / H dismiss
export function resolveHelpKey(input: string, key: any): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("letterH", input, key)) return [{ type: "dismiss" }];
  return [];
}
