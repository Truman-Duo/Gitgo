// src/input/overlays/formal.ts — FormalPanel key→action resolution.
import { matchChord } from "../bindings.js";

export type FormalAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "delete" }
  | { type: "dissolve" }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveFormalKey(confirm: boolean, input: string, key: any): FormalAction[] {
  if (confirm) {
    if (matchChord("escape", input, key)) return [{ type: "confirmCancel" }];
    if (matchChord("up", input, key)) return [{ type: "confirmMove", index: 0 }];
    if (matchChord("down", input, key)) return [{ type: "confirmMove", index: 1 }];
    if (matchChord("enter", input, key)) return [{ type: "confirmYes" }];
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterD", input, key)) return [{ type: "delete" }];
  if (matchChord("letterX", input, key)) return [{ type: "dissolve" }];
  return [];
}
