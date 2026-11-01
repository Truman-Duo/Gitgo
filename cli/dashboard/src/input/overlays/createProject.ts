import { matchChord } from "../bindings.js";
import type { TextOp } from "../keymap.js";
export type CreateProjectAction = {
    type: "dismiss" | "nextField" | "prevField" | "submit" | "beginEdit" | "confirmEdit" | "cancelEdit" | "modePrev" | "modeNext";
} | {
    type: "text";
    op: TextOp;
};
export function resolveCreateProjectKey(field: string, input: string, key: any, editing = false): CreateProjectAction[] {
    if (matchChord("formSave", input, key))
        return [{ type: "submit" }];
    if (matchChord("escape", input, key))
        return [{ type: editing ? "cancelEdit" : "dismiss" }];
    if (matchChord("enter", input, key))
        return [{ type: editing ? "confirmEdit" : "beginEdit" }];
    if (!editing) {
        if (matchChord("shiftTab", input, key) || matchChord("up", input, key))
            return [{ type: "prevField" }];
        if (matchChord("tab", input, key) || matchChord("down", input, key))
            return [{ type: "nextField" }];
        return [];
    }
    if (field === "mode") {
        if (matchChord("left", input, key))
            return [{ type: "modePrev" }];
        if (matchChord("right", input, key))
            return [{ type: "modeNext" }];
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
