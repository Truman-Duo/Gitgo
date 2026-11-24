import React from "react";
import {Box, Text} from "@anthropic/ink";
import {useDisclosureAction} from '../hooks/useDisclosureAction.js';
import {useSharedClock} from '../hooks/useSharedClock.js';
import {colors} from "../theme/index.js";
import {SelectionText} from "./Selection.js";
import {AnimatedReveal} from "./AnimatedReveal.js";

/** A controlled record. The owning scene handles keyboard selection/navigation. */
export function Disclosure({label, expanded, onToggle, selected = false, focused = true, headerInset = 0, contentInset = 0, expandable = true, running = false, children}: {
  label: React.ReactNode; expanded: boolean; onToggle: () => void;
  selected?: boolean; focused?: boolean; headerInset?: number; contentInset?: number; children: React.ReactNode;
  expandable?: boolean; running?: boolean;
}) {
  const {header,toggle}=useDisclosureAction(expanded,onToggle);
  const marker = expandable ? expanded ? "▾" : "▸" : " ";
  return <Box flexDirection="column" flexShrink={0}>
    <Box ref={header} onClick={expandable ? toggle : undefined} clickMode="action" paddingLeft={headerInset} minHeight={1} flexShrink={0}>
      <SelectionText selected={selected} focused={focused} dimColor={!selected} wrap="truncate-end">
        {running ? <RunningMarker glyph={expandable ? marker : "•"}/> : marker}{" "}{label}
      </SelectionText>
    </Box>
    <AnimatedReveal expanded={expanded}>
      <Box flexDirection="column" paddingLeft={contentInset}>{children}</Box>
    </AnimatedReveal>
  </Box>;
}

function RunningMarker({glyph}: {glyph: string}) {
  const period = colors.spinner.activityPulseMs;
  const now = useSharedClock(period);
  const color = Math.floor(now / period) % 2 === 0 ? colors.warning : colors.spinner.activityPulseLow;
  return <Text color={color}>{glyph}</Text>;
}
