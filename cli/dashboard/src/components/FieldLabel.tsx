import React, {useEffect, useState} from "react";
import {colors, displayWidth} from "../theme/index.js";
import {SelectionText} from "./Selection.js";

/** Only the field name blinks; its column, value and selection stay stationary. */
export function FieldLabel({label, suffix = ": ", selected, editing}: {
  label: string; suffix?: string; selected: boolean; editing: boolean;
}) {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    setVisible(true);
    if (!editing) return;
    const timer = setInterval(() => setVisible(value => !value), colors.form.blinkMs);
    return () => clearInterval(timer);
  }, [editing, label]);
  return <SelectionText selected={selected} wrap="truncate-end">
    {!editing || visible ? label : " ".repeat(displayWidth(label))}{suffix}
  </SelectionText>;
}
