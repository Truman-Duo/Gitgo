// src/theme/useSelectionStyle.ts — Unified selection style hook.
// Covers all 6 selection patterns × focused/non-focused context.

import type { SelectionContext, SelectionVariant, StyleProps } from "./types.js";
import { colors } from "./tokens.js";

const ACCENT_MAP: Record<string, { bg: string; fg: string }> = {
  accent: colors.selection.block.blue,
  success: colors.selection.block.green,
  warning: { bg: colors.warning, fg: colors.selection.block.fg },
  danger: { bg: colors.danger, fg: colors.selection.block.fg },
  silver: colors.selection.block.silver,
  "confirm-yes": { bg: colors.confirm.yes.bg, fg: colors.confirm.yes.fg },
  "confirm-no": { bg: colors.confirm.no.bg, fg: colors.confirm.no.fg },
};

export function useSelectionStyle(
  context: SelectionContext,
  variant: SelectionVariant,
  accentColor?: string,
): StyleProps {
  // Non-focused context
  if (context === "non-focused") {
    if (variant === "row") {
      return { bg: undefined, fg: undefined, bold: false };
    }
    return { bg: colors.selection.dim.block.bg, fg: undefined, bold: false };
  }

  // Focused context
  if (variant === "row") {
    return {
      bg: colors.selection.row.bg,
      fg: colors.selection.row.fg,
      bold: true,
    };
  }

  // Focused + edit-field — form field active indicator
  if (variant === "edit-field") {
    return {
      bg: colors.edit.field.activeBg,
      fg: colors.edit.field.activeFg,
      bold: true,
    };
  }

  // Focused + block — resolve accent color or default white block
  if (accentColor && ACCENT_MAP[accentColor]) {
    const block = ACCENT_MAP[accentColor];
    return { bg: block.bg, fg: block.fg, bold: true };
  }

  // Default block (white)
  return {
    bg: colors.selection.block.bg,
    fg: colors.selection.block.fg,
    bold: true,
  };
}
