// src/theme/types.ts — Shared types for the theme system.

import type { Color } from "@anthropic/ink";

export type SemanticColor = "success" | "warning" | "danger" | "accent";
export type SelectionContext = "focused" | "non-focused";
export type SelectionVariant = "row" | "block" | "edit-field";
export type InputMode = "NORMAL" | "COMMAND";
export type StatusState = "error" | "warning" | "ok" | "offline" | "done";

export interface StyleProps {
  bg: Color | undefined;
  fg: Color | undefined;
  bold: boolean;
}

export interface PanelSize {
  cols: number;
  rows: number;
  w: number;
  h: number;
}

export interface StatusDot {
  char: string;
  color: Color | undefined;
  badgeBg: Color | undefined;
}
