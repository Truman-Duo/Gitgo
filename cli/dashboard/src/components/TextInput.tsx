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
import React, { useEffect,useMemo } from "react";
import { Text, Box, Ansi, useDeclaredCursor, stringWidth, wrapText } from "@anthropic/ink";
import type { Color } from "@anthropic/ink";
import {highlightCode} from "../theme/syntax.js";
import type {DocumentRow} from "../input/documentLayout.js";
import sliceAnsi from "../../vendor/ink/src/core/utils/sliceAnsi.js";

type Props = {
  value: string;
  cursorOffset: number;
  placeholder?: string;
  focus?: boolean;
  showCursor?: boolean;
  color?: Color;
  dimColor?: boolean;
  maxWidth?: number;
  syntaxLanguage?: string;
  document?:{rows:DocumentRow[];top:number;height:number;caret:{row:number;column:number}};
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
  syntaxLanguage,
  document,
}: Props) {
  const cursor = Math.max(0, Math.min(cursorOffset, value.length));
  const active = focus && (Boolean(document) || showCursor);
  const visible = showCursor && (document?document.caret.row>=document.top&&document.caret.row<document.top+document.height:value.length>0);
  const highlighted=useMemo(()=>syntaxLanguage?highlightCode(value,syntaxLanguage,false):value,[value,syntaxLanguage]);

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
  if(document){cursorLine=document.caret.row-document.top;cursorColumn=document.caret.column;}

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

  if(document){
    const logical=highlighted.split("\n"),shown=document.rows.slice(document.top,document.top+document.height);
    return <Box ref={cursorRef} flexDirection="column">{shown.map((row,index)=><Box key={index} height={1} flexShrink={0}>
      <Ansi>{(syntaxLanguage?sliceAnsi(logical[row.line] || "",row.column,row.column+row.width):row.text) || " "}</Ansi>
    </Box>)}</Box>;
  }
  if (value.length === 0) {
    return (
      <Box ref={cursorRef}>
        <Text dimColor>{placeholder || " "}</Text>
      </Box>
    );
  }

  // Styling never enters the editable value or cursor-offset calculations.
  // Use the same language/theme mapping as Markdown and diff code rendering.
  if(syntaxLanguage)return <Box ref={cursorRef}><Ansi>{highlighted+(cursor===value.length?" ":"")}</Ansi></Box>;

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
