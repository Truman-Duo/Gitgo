import { matchChord } from "../bindings.js";
import type { TextOp } from "../keymap.js";
export type ExportAction = {
    type: "dismiss" | "beginEdit" | "confirm" | "cancelEdit" | "submit";
} | {
    type: "moveField";
    delta: number;
} | {
    type: "cycle";
    delta: number;
} | {
    type: "text";
    op: TextOp;
};
export function resolveExportKey(status: "idle" | "exporting" | "done" | "error", field: "scope" | "format" | "destination", input: string, key: any, editing = false): ExportAction[] {
    if (status === "exporting")
        return [];
    if (status === "done")
        return matchChord("enter", input, key) || matchChord("escape", input, key) ? [{ type: "dismiss" }] : [];
    if (matchChord("formSave", input, key))
        return [{ type: "submit" }];
    if (matchChord("escape", input, key))
        return [{ type: editing ? "cancelEdit" : "dismiss" }];
    if (matchChord("enter", input, key))
        return [{ type: editing ? "confirm" : "beginEdit" }];
    if (!editing) {
        if (matchChord("up", input, key) || matchChord("shiftTab", input, key))
            return [{ type: "moveField", delta: -1 }];
        if (matchChord("down", input, key) || matchChord("tab", input, key))
            return [{ type: "moveField", delta: 1 }];
        return [];
    }
    if (field !== "destination") {
        if (matchChord("left", input, key))
            return [{ type: "cycle", delta: -1 }];
        if (matchChord("right", input, key))
            return [{ type: "cycle", delta: 1 }];
        return [];
    }
    if (matchChord("backspace", input, key))
        return [{ type: "text", op: { op: "delete_back" } }];
    if (matchChord("delete", input, key))
        return [{ type: "text", op: { op: "delete_forward" } }];
    if (matchChord("left", input, key))
        return [{ type: "text", op: { op: "move_cursor", delta: -1 } }];
    if (matchChord("right", input, key))
        return [{ type: "text", op: { op: "move_cursor", delta: 1 } }];
    if (matchChord("home", input, key))
        return [{ type: "text", op: { op: "move_to_start" } }];
    if (matchChord("end", input, key))
        return [{ type: "text", op: { op: "move_to_end" } }];
    if (input && !key.ctrl && !key.meta)
        return [{ type: "text", op: { op: "insert", text: input } }];
    return [];
}
