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

  const w = Math.max(minW, (columns ?? 80) - wOff);
  const h = Math.max(minH, (rows ?? 24) - hOff);
  const contentH = h - header - footer;

  return {
    cols: columns ?? 80,
    rows: rows ?? 24,
    w,
    h,
    contentH,
    needsVerticalScroll: (contentRows: number) => contentRows > contentH,
    maxVisibleItems: (contentRows: number) => Math.max(5, contentH),
  };
}
