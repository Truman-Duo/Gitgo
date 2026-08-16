// src/input/overlays/export.ts — ExportPanel key→action resolution.
import { matchChord } from "../bindings.js";
import type { OverlayAction } from "./actions.js";

// Esc dismiss; selection/confirm only while idle/error.
export function resolveExportKey(
  status: "idle" | "exporting" | "done" | "error",
  input: string,
  key: any,
): OverlayAction[] {
  if (matchChord("escape", input, key)) return [{ type: "dismiss" }];
  if (status === "exporting") return [];
  if (status === "done") return [{ type: "dismiss" }];
  if (matchChord("up", input, key)) return [{ type: "move", delta: -1 }];
  if (matchChord("down", input, key)) return [{ type: "move", delta: 1 }];
  if (matchChord("enter", input, key)) return [{ type: "confirm" }];
  return [];
}
