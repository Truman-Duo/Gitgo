import {useLayoutEffect, useRef, useState} from "react";
import {useTextInput} from "./useTextInput.js";
import type {PublishLocalState} from "../state/publishLocalState.js";

type SavedForm = {values: Record<string, string>; field: string; cursor: number; context: string};
export function usePublishForm(state: PublishLocalState | undefined, key: string,
  defaults: Record<string, string>, context: string, fieldsFor: (values: Record<string, string>) => string[],
  options: {persistPristine?: boolean} = {}) {
  const [saved] = useState(() => state?.get<SavedForm>(key));
  const [values, setValues] = useState(() => ({...defaults, ...Object.fromEntries(Object.entries(saved?.values ?? {})
    .filter(([name, value]) => name in defaults && name !== "token" && typeof value === "string"))}));
  const fields = fieldsFor(values);
  const [index, setIndex] = useState(() => Math.max(0, fields.indexOf(saved?.field ?? "")));
  const field = fields[Math.min(index, fields.length - 1)];
  const input = useTextInput(values[field] ?? "");
  const [boundContext, setBoundContext] = useState(saved?.context ?? context);
  const first = useRef(true), previous = useRef(field);
  useLayoutEffect(() => {
    if (first.current || previous.current !== field) {
      const value = values[field] ?? "";
      input.setValue(value);
      if (first.current && saved?.field === field && field !== "token") input.moveCursor(Math.min(value.length, Math.max(0, saved.cursor || 0)) - value.length);
      first.current = false; previous.current = field;
    }
  }, [field]);
  useLayoutEffect(() => {
    const current = {...values, [field]: input.materialize()};
    // Sections are visible before entering the form. Browsing current settings
    // must not save an untouched snapshot as a future stale editing draft.
    if (options.persistPristine === false && Object.keys(defaults).every(name => current[name] === defaults[name])) {state?.remove(key); return;}
    state?.set(key, {values: {...current, token: undefined}, field, cursor: input.cursor, context: boundContext});
  }, [state, key, values, field, input.value, input.cursor, boundContext, options.persistPristine]);
  const accept = (next: Record<string, string>, nextContext: string) => {
    setValues(next); setBoundContext(nextContext); input.setValue(next[field] ?? "");
  };
  const reset = () => { setValues(defaults); setIndex(0); setBoundContext(context); input.setValue(defaults[fieldsFor(defaults)[0]] ?? ""); };
  return {values, setValues, fields, index, setIndex, field, input, stale: boundContext !== context, reset, accept};
}
