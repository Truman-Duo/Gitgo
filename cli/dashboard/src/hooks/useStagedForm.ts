import {useState} from "react";
import type {UseTextInputReturn} from "./useTextInput.js";

/** Field confirmation stages a draft; only the owner's save persists it. */
export function useStagedForm<T extends Record<string, string>>(
  initial: T, fields: readonly (keyof T)[], choices: readonly (keyof T)[], input: UseTextInputReturn,
) {
  const [values, setValues] = useState<T>(initial);
  const [fieldIdx, setFieldIdx] = useState(0);
  const [editing, setEditing] = useState(false);
  const [choice, setChoice] = useState("");
  const field = fields[fieldIdx]!;
  const isChoice = choices.includes(field);
  const current = editing ? {...values, [field]: isChoice ? choice : input.value} : values;
  const stop = () => {setEditing(false); input.setValue("");};
  return {
    values, current, field, fieldIdx, editing, isChoice, setChoice,
    begin() {setChoice(values[field]); input.setValue(isChoice ? "" : values[field]); setEditing(true);},
    confirm(next: T = current) {setValues(next); stop();},
    cancel: stop,
    move(delta: number) {if (!editing) setFieldIdx(index => Math.max(0, Math.min(fields.length - 1, index + delta)));},
    reset(next: T) {setValues(next); setFieldIdx(0); stop();},
  };
}
