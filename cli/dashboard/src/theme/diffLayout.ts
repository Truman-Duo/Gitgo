// src/theme/diffLayout.ts — Diff geometry, shared by every diff renderer.
// Pure functions: terminal width → column math. No React, no per-call-site coupling.

import { truncate } from "./typography.js";

/**
 * Horizontal chars consumed *outside* the diff content area:
 * Diff frame border (2) + its padding-left (1). Parent components pass their
 * actual allocated content width; nested panel padding must not be guessed here.
 */
export const DIFF_FRAME_OVERHEAD = 3;

/** Available content width inside the diff frame at a given panel width. */
export function diffAvail(panelWidth: number): number {
  return Math.max(30, panelWidth - DIFF_FRAME_OVERHEAD);
}

/** Right-aligned line number (no trailing space). */
export function diffLineNo(lineNo: number | null, digits: number): string {
  return lineNo == null ? " ".repeat(digits) : String(lineNo).padStart(digits);
}

/** Single-column (unified) content width: [old][gap][new][gap][cell]. */
export function unifiedColWidth(avail: number, digits: number): number {
  return Math.max(10, avail - 2 * digits - 3);
}

/** Per-side content width for side-by-side: [num][gap][cell][gap][│][gap][num][gap][cell]. */
export function splitColWidth(avail: number, digits: number): number {
  return Math.max(8, Math.floor((avail - 2 * digits - 6) / 2));
}

/** A split row is useful only while both code cells retain readable width. */
export function canUseSplitDiff(avail: number, digits: number): boolean {
  return splitColWidth(avail, digits) >= 18;
}

/** `sign` + one space + ellipsis-truncated content, sized for a fixed-width cell. */
export function diffCell(sign: string, text: string | null, width: number): string {
  if (text == null) return sign === " " ? "" : sign;
  return sign + " " + truncate(text, Math.max(1, width - 2));
}
