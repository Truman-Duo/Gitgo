// src/theme/diffLayout.ts — Diff geometry, shared by every diff renderer.
// Pure functions: terminal width → column math. No React, no per-call-site coupling.

import { truncate } from "./typography.js";

/**
 * Horizontal chars consumed *outside* the diff content area:
 * parent panel padding-left (1) + frame border (2) + frame padding-left (1).
 * Kept here (not in a component) so every diff renderer shares the same frame math.
 */
export const DIFF_FRAME_OVERHEAD = 4;

/** Available content width inside the diff frame at a given panel width. */
export function diffAvail(panelWidth: number): number {
  return Math.max(30, panelWidth - DIFF_FRAME_OVERHEAD);
}

/** Right-aligned line number (no trailing space). */
export function diffLineNo(lineNo: number | null, digits: number): string {
  return lineNo == null ? " ".repeat(digits) : String(lineNo).padStart(digits);
}

/** Single-column (unified) content width: [num][gap][cell]. */
export function unifiedColWidth(avail: number, digits: number): number {
  return Math.max(10, avail - digits - 2);
}

/** Per-side content width for side-by-side: [num][gap][cell][gap][│][gap][num][gap][cell]. */
export function splitColWidth(avail: number, digits: number): number {
  return Math.max(8, Math.floor((avail - 2 * digits - 6) / 2));
}

/** `sign` + one space + ellipsis-truncated content, sized for a fixed-width cell. */
export function diffCell(sign: string, text: string | null, width: number): string {
  if (text == null) return sign === " " ? "" : sign;
  return sign + " " + truncate(text, Math.max(1, width - 2));
}
