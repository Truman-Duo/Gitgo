// src/input/overlays/lessons.ts — LessonsPanel key→action resolution.
import { matchChord } from "../bindings.js";

export type LessonsAction =
  | { type: "dismiss" }
  | { type: "move"; delta: number }
  | { type: "searchMode" }
  | { type: "verify" }
  | { type: "searchBack" }
  | { type: "searchRun" }
  | { type: "searchBackspace" }
  | { type: "searchInsert"; text: string };

export function resolveLessonsKey(mode: "list" | "search", input: string, key: any): LessonsAction[] {
  if (mode === "search") {
    if (matchChord("escape", input, key)) return [{ type: "searchBack" }];
    if (matchChord("enter", input, key)) return [{ type: "searchRun" }];
    if (matchChord("backspace", input, key)) return [{ type: "searchBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "searchInsert", text: input }];
    }
    return [];
  }
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("letterV", input, key)) return [{ type: "verify" }];
  if (matchChord("letterS", input, key)) return [{ type: "searchMode" }];
  return [];
}
