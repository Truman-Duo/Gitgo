// src/input/overlays/actions.ts — shared overlay action vocabulary.
// Simple overlays (help/quit/status/runtimeMenu/export/inlineContext/dialogSelect)
// resolve keys to this small action union; richer overlays define their own.
import type { TextOp } from "../keymap.js";

export type OverlayAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "confirm" }
  | { type: "text"; op: TextOp };
