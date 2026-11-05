import React, {useEffect, useRef, useState} from "react";
import {Box, Text} from "@anthropic/ink";
import {useManagedInput} from "../input/runtime.js";
import {matchChord, chordLabel} from "../input/bindings.js";
import {applyTextOp} from "./useTextInput.js";
import type {usePublishForm} from "./usePublishForm.js";
import type {FooterConfig} from "../components/CommandBar.js";
import {usePanelSize} from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";
import {FormFieldRow, fieldLabelWidth} from "../components/FormFieldRow.js";
import {SelectionText} from "../components/Selection.js";
import {cycleChoiceIndex} from "../input/choice.js";
import {useLocalHelpCommand} from "./useLocalHelpCommand.js";

type Form = ReturnType<typeof usePublishForm>;
/** A form owns one footer only while a text field is explicitly being edited. */
export function usePublishFormEditor(form: Form, {busy, onFooter, onDismiss, onSave, choices = {}, actions = {}, isActive = true}: {
  busy: boolean; onFooter: (footer: FooterConfig | null) => void; onDismiss: () => void;
  onSave: (values: Record<string, string>) => void | Promise<void>; choices?: Record<string, string[]>; actions?: Record<string, () => void>; isActive?: boolean;
}) {
  const {values, setValues, fields, index, setIndex, field, input, stale, reset} = form;
  const t = useCopy(), {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [editing, setEditing] = useState(false), [option, setOption] = useState<number | null>(null);
  const saving = useRef(false);
  const help=useLocalHelpCommand("publish.settings",onFooter,isActive&&!editing&&option===null&&!saving.current);
  useEffect(() => {
    if(help.active)return;
    onFooter(editing && isActive ? {kind: "normal", cmdInput: field === "token" ? {...input, value: "•".repeat(input.value.length)} : input,
      statusText: "",
      suggestions: [], suggestionIdx: 0, cmdResult: ""} : {hidden: true});
    return () => onFooter(null);
  }, [editing, field, input.value, input.cursor, onFooter, t, isActive,help.active]);
  const current = () => ({...values, [field]: option !== null ? choices[field]![option]! : input.materialize()});
  useManagedInput((text, key) => {
    if(help.handle(text,key))return;
    if (busy || saving.current) return;
    if (matchChord("formSave", text, key)) {
      if (!stale) {
        const next = current(); setValues(next); input.setValue(next[field]!); setEditing(false); setOption(null); saving.current = true;
        try {void Promise.resolve(onSave(next)).finally(() => {saving.current = false;});}
        catch (error) {saving.current = false; throw error;}
      }
      return;
    }
    if (key.ctrl && text === "r") { reset(); setEditing(false); setOption(null); return; }
    if (option !== null) {
      const available = choices[field] ?? [];
      if (matchChord("escape", text, key)) setOption(null);
      else if (matchChord("left", text, key)) setOption(value => cycleChoiceIndex(value!, -1, available.length));
      else if (matchChord("right", text, key)) setOption(value => cycleChoiceIndex(value!, 1, available.length));
      else if (matchChord("enter", text, key)) { input.setValue(available[option]!); setValues({...values, [field]: available[option]!}); setOption(null); }
      return;
    }
    if (!editing) {
      if (matchChord("escape", text, key)) onDismiss();
      else if (matchChord("up", text, key)) setIndex(Math.max(0, index - 1));
      else if (matchChord("down", text, key)) setIndex(Math.min(fields.length - 1, index + 1));
      else if (matchChord("home", text, key)) setIndex(0);
      else if (matchChord("end", text, key)) setIndex(fields.length - 1);
      else if (matchChord("enter", text, key)) {
        if (actions[field]) actions[field]!();
        else if (choices[field]?.length) setOption(Math.max(0, choices[field]!.indexOf(values[field] ?? "")));
        else setEditing(true);
      }
      return;
    }
    if (matchChord("escape", text, key)) {input.setValue(values[field] ?? ""); setEditing(false); return;}
    if (matchChord("enter", text, key)) { setValues(current()); setEditing(false); return; }
    if (key.newline || key.return && key.shift) applyTextOp({op: "insert", text: "\n"}, input);
    else if (matchChord("backspace", text, key)) applyTextOp({op: "delete_back"}, input);
    else if (matchChord("delete", text, key)) applyTextOp({op: "delete_forward"}, input);
    else if (matchChord("left", text, key)) applyTextOp({op: "move_cursor", delta: -1}, input);
    else if (matchChord("right", text, key)) applyTextOp({op: "move_cursor", delta: 1}, input);
    else if (matchChord("home", text, key)) applyTextOp({op: "move_to_start"}, input);
    else if (matchChord("end", text, key)) applyTextOp({op: "move_to_end"}, input);
    else if (key.ctrl && text === "u") input.setValue("");
    else if (text && !key.ctrl && !key.meta) applyTextOp({op: "insert", text}, input);
  }, {isActive, bindingContext: "form",clipboard:editing&&option===null,inputTarget:editing?field:""});
  const rows = (label: (field: string) => string, start = 0, count = fields.length) => <>
    {fields.slice(start, start + count).map((name, offset) => {
      const selected = start + offset === index, active = selected && isActive;
      const value = active && option !== null ? choices[field]![option]! : selected ? input.value : values[name] ?? "";
      return <FormFieldRow key={name} label={label(name)} labelWidth={fieldLabelWidth(fields.map(label), Math.max(1, w - 10))} selected={active} editing={active && (editing || option !== null)}>
        <SelectionText wrap="truncate-end">
          {value ? name === "token" ? "•".repeat(value.length) : value.replace(/[\x00-\x1f\x7f-\x9f]/g, " ") : "—"}
        </SelectionText>
      </FormFieldRow>;
    })}

  </>;
  return {editing, rows};
}
