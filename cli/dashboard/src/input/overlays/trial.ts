// src/input/overlays/trial.ts — TrialPanel key→action resolution.
import { matchChord } from "../bindings.js";

export type TrialAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "triage"; action: string }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveTrialKey(confirm: boolean, input: string, key: any): TrialAction[] {
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
  if (matchChord("letterA", input, key)) return [{ type: "triage", action: "accept" }];
  if (matchChord("letterP", input, key)) return [{ type: "triage", action: "promote" }];
  if (matchChord("letterD", input, key)) return [{ type: "triage", action: "discard" }];
  return [];
}
