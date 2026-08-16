// src/input/overlays/memory.ts — MemoryPanel key→action resolution.
import { matchChord } from "../bindings.js";

export type MemoryAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "snapshot" }
  | { type: "restore" }
  | { type: "confirmCancel" }
  | { type: "confirmMove"; index: number }
  | { type: "confirmYes" };

export function resolveMemoryKey(confirm: boolean, input: string, key: any): MemoryAction[] {
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
  if (matchChord("letterS", input, key)) return [{ type: "snapshot" }];
  if (matchChord("letterR", input, key)) return [{ type: "restore" }];
  return [];
}
