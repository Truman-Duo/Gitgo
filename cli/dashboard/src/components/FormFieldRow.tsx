import React from "react";
import {Box} from "@anthropic/ink";
import {displayWidth} from "../theme/index.js";
import {FieldLabel} from "./FieldLabel.js";
import {SelectionRow} from "./Selection.js";
import {geometry} from "../theme/tokens.js";

export function fieldLabelWidth(labels: string[], maximum = Infinity, suffix = ": ") {
  return Math.max(1, Math.min(maximum, Math.max(0, ...labels.map(label => displayWidth(label + suffix))) + geometry.field.gap));
}

/** No leading marker. Widths are terminal cells, shared by every field in a group. */
export function FormFieldRow({label, selected, editing = false, disabled = false, labelWidth, suffix = ": ", highlight = true, children}: {
  label: string; selected: boolean; editing?: boolean; disabled?: boolean; labelWidth?: number; suffix?: string; highlight?: boolean; children: React.ReactNode;
}) {
  const gap = labelWidth === undefined ? geometry.field.gap : Math.min(geometry.field.gap, Math.max(0, labelWidth - 1));
  return <SelectionRow selected={selected} disabled={disabled} highlight={highlight} flexShrink={0}>
    <Box width={labelWidth === undefined ? undefined : Math.max(1, labelWidth - gap)} flexShrink={0}><FieldLabel label={label} suffix={suffix} selected={selected} editing={selected && editing && !disabled}/></Box>
    <Box width={gap} flexShrink={0}/>
    <Box flexGrow={1} minWidth={0}>{children}</Box>
  </SelectionRow>;
}
