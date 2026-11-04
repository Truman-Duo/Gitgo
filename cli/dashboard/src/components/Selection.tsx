import React, {createContext, forwardRef, useContext, useMemo, type ComponentProps} from "react";
import {Box, Text, type DOMElement} from "@anthropic/ink";
import {colors, useSelectionStyle} from "../theme/index.js";
import type {SelectionVariant} from "../theme/types.js";
import {geometry} from "../theme/tokens.js";

type State = {selected: boolean; focused?: boolean; disabled?: boolean; muted?: boolean; variant?: SelectionVariant | "suggestion"; accent?: string; highlight?: boolean};
const SelectionContext = createContext<State>({selected: false});

function styleFor({selected, focused = true, disabled = false, muted = false, variant = "row", accent, highlight = true}: State) {
  if (!selected || disabled) return {bg: undefined, fg: muted ? colors.named.gray : undefined, bold: false};
  // Selection and keyboard focus are separate facts. Moving into a page's
  // content can dim its header block, but cannot deselect or unbold that tab.
  const style = variant === "suggestion" ? {...colors.suggestion.active, bold: true}
    : useSelectionStyle(focused ? "focused" : "non-focused", variant, accent);
  return {...style, fg: muted ? colors.named.gray : style.fg, bg: highlight ? style.bg : undefined, bold: true};
}

type TextProps = Omit<ComponentProps<typeof Text>, "bold" | "backgroundColor">;
export function SelectionText({selected, focused, disabled, muted, variant, accent, highlight, color, dimColor, ...props}:
  TextProps & Partial<State>) {
  const parent = useContext(SelectionContext);
  const state = {selected: selected ?? parent.selected, focused: focused ?? parent.focused,
    disabled: disabled ?? parent.disabled, muted: muted ?? parent.muted, variant: variant ?? parent.variant, accent: accent ?? parent.accent,
    highlight: highlight ?? parent.highlight};
  const style = styleFor(state);
  return <Text {...props} color={style.fg ?? color} backgroundColor={style.bg} bold={style.bold}
    dimColor={state.disabled || (!state.selected && dimColor)}/>;
}

export const SelectionRow = forwardRef<DOMElement, Omit<ComponentProps<typeof Box>, "backgroundColor"> & State & {highlight?: boolean}>(
  function SelectionRow({selected, focused = true, disabled = false, muted = false, variant = "row", accent, highlight = true, children, ...props}, ref) {
    const state = useMemo(()=>({selected, focused, disabled, muted, variant, accent, highlight}), [selected, focused, disabled, muted, variant, accent, highlight]);
    const style = styleFor(state);
    return <SelectionContext.Provider value={state}><Box minWidth={0} minHeight={geometry.row.minHeight} flexShrink={0} {...props} ref={ref} backgroundColor={highlight ? style.bg : undefined}>
      {children}
    </Box></SelectionContext.Provider>;
  });

/** The same text-only selection and indentation in commands and inline choices. */
export function SuggestionRow({selected,disabled=false,label,description,tone="command"}:{selected:boolean;disabled?:boolean;label:React.ReactNode;description?:React.ReactNode;tone?:"command"|"neutral"}){
 return <Box flexDirection="row" paddingLeft={geometry.choice.rowInset} minHeight={geometry.row.minHeight} flexShrink={0}>
  <SelectionText selected={selected} disabled={disabled} variant={tone==="command"?"suggestion":"row"} dimColor={!selected} wrap="truncate-end">{label}</SelectionText>
  {description?<Text dimColor={!selected}>  {description}</Text>:null}
 </Box>;
}

/** Table headings are semantic emphasis, independent of row selection/focus. */
export function TableHeaderText(props: Omit<TextProps, "dimColor"> & {dimColor?: boolean}) {
  return <Text {...props} bold/>;
}
