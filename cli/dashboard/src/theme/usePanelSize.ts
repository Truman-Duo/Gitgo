// src/theme/usePanelSize.ts — Unified responsive sizing hook (horizontal + vertical).

import { useTerminalSize } from "@anthropic/ink";
import type { PanelSize } from "./types.js";

export function usePanelSize(opts?: {
  minWidth?: number;
  widthOffset?: number;
  minHeight?: number;
  heightOffset?: number;
  headerRows?: number;
  footerRows?: number;
}): PanelSize & {
  contentH: number;
  needsVerticalScroll: (contentRows: number) => boolean;
  maxVisibleItems: (contentRows: number) => number;
} {
  const { columns, rows } = useTerminalSize();
  const minW = opts?.minWidth ?? 40;
  const wOff = opts?.widthOffset ?? 0;
  const minH = opts?.minHeight ?? 10;
  const hOff = opts?.heightOffset ?? 0;
  const header = opts?.headerRows ?? 0;
  const footer = opts?.footerRows ?? 0;

  // Minimums are useful fallbacks when no terminal size is available. Once
  // known, the physical viewport is the ceiling, including on short screens.
  const cols = columns ?? Math.max(80, minW);
  const terminalRows = rows ?? Math.max(24, minH);
  const w = Math.max(1, cols - wOff);
  const h = Math.max(1, terminalRows - hOff);
  const contentH = Math.max(1, h - header - footer);

  return {
    cols,
    rows: terminalRows,
    w,
    h,
    contentH,
    needsVerticalScroll: (contentRows: number) => contentRows > contentH,
    maxVisibleItems: (contentRows: number) => Math.min(Math.max(0, contentRows), contentH),
  };
}
