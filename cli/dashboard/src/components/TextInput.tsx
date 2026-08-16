// src/components/TextInput.tsx — Renders text with native terminal cursor at caret.
// useDeclaredCursor positions the terminal's own cursor — IME preedit renders
// at the physical cursor position.
//
// active=true keeps the cursor declaration alive so IME can read the position.
// visible=false hides the cursor without clearing the declaration — Windows
// Terminal (PR #17181), iTerm2, and Kitty render IME preedit at the declared
// position even when the cursor is hidden.
//
// Single-line rendering with native terminal cursor. The terminal auto-wraps
// long text; we compute the wrapped cursor position via wrapText() and declare
// it to Ink so IME sees the correct screen position. No layout transitions =
// IME stable.
import React, { useEffect } from "react";
import { Text, Box, useDeclaredCursor, stringWidth, wrapText } from "@anthropic/ink";

type Props = {
  value: string;
  cursorOffset: number;
  placeholder?: string;
  focus?: boolean;
  showCursor?: boolean;
  color?: string;
  dimColor?: boolean;
  maxWidth?: number;
};

export const TextInput = function TextInput({
  value,
  cursorOffset,
  placeholder = "",
  focus = true,
  showCursor = true,
  color,
  dimColor: dim,
  maxWidth,
}: Props) {
  const cursor = Math.max(0, Math.min(cursorOffset, value.length));
  const active = focus && showCursor;
  const visible = value.length > 0;

  // Compute cursor (line, column) in the wrapped text layout.
  // wrapText is the same function the renderer uses, so wrapping the
  // prefix (text before cursor) tells us exactly which line and column
  // the cursor lands on after terminal/renderer wrapping.
  let cursorLine = 0;
  let cursorColumn = 0;

  if (value.length > 0) {
    const effectiveWidth =
      maxWidth && maxWidth > 0 ? maxWidth : Math.max(60, process.stdout.columns || 80);
    const prefix = value.slice(0, cursor);
    const prefixWrapped = wrapText(prefix, effectiveWidth, "wrap");
    const prefixLines = prefixWrapped.split("\n");
    cursorLine = prefixLines.length - 1;
    cursorColumn = stringWidth(prefixLines[cursorLine]!);
  }

  const cursorRef = useDeclaredCursor({
    line: cursorLine,
    column: cursorColumn,
    active,
    visible,
  });

  // Direct stdout write: guarantee terminal cursor is visible when active.
  useEffect(() => {
    if (active && visible && process.stdout.isTTY) {
      process.stdout.write("\x1b[?25h");
      return () => {
        process.stdout.write("\x1b[?25l");
      };
    }
  }, [active, visible]);

  if (value.length === 0) {
    return (
      <Box ref={cursorRef}>
        <Text dimColor>{placeholder || " "}</Text>
      </Box>
    );
  }

  const before = value.slice(0, cursor);
  const at = value[cursor] || " ";
  const after = value.slice(cursor + 1);

  return (
    <Box ref={cursorRef}>
      <Text color={color} dimColor={dim}>
        {before}
        {at}
        {after}
      </Text>
    </Box>
  );
};
