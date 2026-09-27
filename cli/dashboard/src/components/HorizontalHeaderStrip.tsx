import React from "react";
import { Box, Text } from "@anthropic/ink";
import { colors, displayWidth } from "../theme/index.js";

export type HeaderStripItem = { id: string; label: string };

export function clampHeaderIndex(index: number, delta: number, count: number): number {
  if (count <= 0) return 0;
  return Math.max(0, Math.min(count - 1, index + delta));
}

export function fitHeaderWindow(items: HeaderStripItem[], selected: number, width: number) {
  if (items.length === 0) return { start: 0, end: 0, left: false, right: false, slotWidth: 5 };
  const active = Math.max(0, Math.min(items.length - 1, selected));
  // Every visible label and every overflow marker occupies one equal header
  // slot.  This preserves the established edge/inter-tab rhythm instead of
  // letting a five-character ellipsis collapse spacing at narrow widths.
  const slotWidth = Math.max(5, ...items.map(item => displayWidth(item.label) + 2));
  const slots = Math.max(1, Math.floor(Math.max(slotWidth, width) / slotWidth));
  if (items.length <= slots) {
    return { start: 0, end: items.length, left: false, right: false, slotWidth };
  }
  let start = Math.max(0, active - Math.floor((slots - 1) / 2));
  let end = Math.min(items.length, start + slots);
  start = Math.max(0, end - slots);
  // Reserve a slot on each truncated side and recalculate around the selected
  // tab.  Navigation remains clamped; the header never forms a circular list.
  for (let pass = 0; pass < 3; pass += 1) {
    const left = start > 0;
    const right = end < items.length;
    const visibleSlots = Math.max(1, slots - Number(left) - Number(right));
    start = Math.max(0, Math.min(active - Math.floor(visibleSlots / 2), items.length - visibleSlots));
    end = Math.min(items.length, start + visibleSlots);
  }
  return { start, end, left: start > 0, right: end < items.length, slotWidth };
}

type Props = {
  items: HeaderStripItem[];
  selected: number;
  width: number;
  contentFocused?: boolean;
};

/** One non-wrapping, width-aware header language for every dashboard page. */
export function HorizontalHeaderStrip({ items, selected, width, contentFocused = false }: Props) {
  const window = fitHeaderWindow(items, selected, width);
  const visible = items.slice(window.start, window.end);
  const slot = (key: string, child: React.ReactNode) =>
    <Box key={key} flexGrow={1} flexBasis={window.slotWidth} justifyContent="center">{child}</Box>;
  return <Box flexDirection="row" width="100%" overflow="hidden">
    {window.left ? slot("overflow-left", <Text dimColor>...</Text>) : null}
    {visible.map((item, offset) => {
      const index = window.start + offset;
      const active = index === selected;
      const bg = active
        ? (contentFocused ? colors.selection.dim.block.bg : colors.selection.block.bg)
        : undefined;
      const fg = active ? colors.selection.block.fg : colors.tab.detail.fg;
      return <Box key={item.id} flexGrow={1} flexBasis={window.slotWidth} justifyContent="center">
        <Text color={fg} backgroundColor={bg} bold={active && !contentFocused} dimColor={!active}>
          {` ${item.label} `}
        </Text>
      </Box>;
    })}
    {window.right ? slot("overflow-right", <Text dimColor>...</Text>) : null}
  </Box>;
}
