// src/theme/typography.ts — Pure-function text utilities.

import type { StatusState, StatusDot } from "./types.js";
import { colors } from "./tokens.js";

/** Truncate string with ellipsis. */
export function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s; // …
}

/** Hard-character text wrapper. Splits on \n, then chops each line at maxW. */
export function wrap(text: string, maxW: number): string[] {
  if (!text) return [""];
  const lines: string[] = [];
  for (const para of text.split("\n")) {
    if (!para) { lines.push(""); continue; }
    let remaining = para;
    while (remaining.length > maxW) {
      lines.push(remaining.slice(0, maxW));
      remaining = remaining.slice(maxW);
    }
    if (remaining) lines.push(remaining);
  }
  return lines;
}

/** Linear interpolate between two hex colors. */
export function lerpColor(a: string, b: string, t: number): string {
  const parseHex = (s: string) => [1, 3, 5].map((i) => parseInt(s.slice(i, i + 2), 16));
  const [r1, g1, b1] = parseHex(a);
  const [r2, g2, b2] = parseHex(b);
  const lerp = (x: number, y: number) => Math.round(x + (y - x) * t);
  return (
    "#" +
    [lerp(r1, r2), lerp(g1, g2), lerp(b1, b2)]
      .map((v) => v.toString(16).padStart(2, "0"))
      .join("")
  );
}

/** Unified status dot (● ◐ ○) with color and badge background. */
export function statusDot(state: StatusState): StatusDot {
  return colors.status[state];
}

/** Form placeholder character (█) — NOT the native terminal cursor. */
export function placeholderChar(visible: boolean): string {
  return visible ? colors.edit.placeholder.char : " ";
}

/** Horizontal separator line. */
export function separator(width: number): string {
  return colors.divider.char.repeat(width);
}

/** Tree-depth indentation. */
export function indent(depth: number): string {
  return "  ".repeat(depth);
}

/** Scroll overflow hint. */
export function scrollHint(hasAbove: boolean, hasBelow: boolean): string {
  if (hasAbove && hasBelow) return "↑↓ scroll"; // ↑↓
  if (hasAbove) return "↑ more"; // ↑
  if (hasBelow) return "↓ more"; // ↓
  return "";
}

/** Semantic color → dark badge background. */
export function badgeBg(semantic: "success" | "warning" | "danger"): string {
  return colors[`${semantic}Badge` as keyof typeof colors] as string;
}

/** Current braille spinner frame. */
export function spinnerFrame(index: number): string {
  const frames = colors.spinner.frames;
  return frames[index % frames.length];
}

/** AgentDetail context bar fill computation. */
export function contextBarFill(
  ratio: number,
  barLen: number,
): { fillColor: string; emptyColor: string; filled: number; empty: number } {
  let fillColor: string;
  if (ratio > 0.8) fillColor = colors.contextBar.high;
  else if (ratio > 0.5) fillColor = colors.contextBar.mid;
  else fillColor = colors.contextBar.low;

  const filled = Math.round(ratio * barLen);
  const empty = barLen - filled;
  return { fillColor, emptyColor: colors.contextBar.empty, filled, empty };
}

/** Model context window in tokens (single source for utilization math). */
const CONTEXT_WINDOW = 128000;

/** Context utilization percentage ("ctx 35%") from a token estimate. Empty for 0/unknown. */
export function contextPct(estimatedTokens: number): string {
  if (!estimatedTokens || estimatedTokens <= 0) return "";
  const pct = Math.round((estimatedTokens / CONTEXT_WINDOW) * 100);
  return `ctx ${pct}%`;
}

/** Map a process status string to its StatusState for statusDot(). */
export function processStatusToDot(s: string): StatusState {
  if (s === "running") return "ok";
  if (s === "completed") return "done";
  if (s === "waiting" || s === "orphaned") return "warning";
  return "error";
}

/** Derive a project's StatusState from daemon liveness + running-process count. */
export function projectStatusDot(daemonOnline: boolean, activeCount: number): StatusState {
  if (!daemonOnline) return "offline";
  return activeCount > 0 ? "ok" : "warning";
}

/** Partition items into running(0)/pending(1)/finished(2) buckets by a rank fn,
 *  optionally sorting each bucket by sortKey. `flat` concatenates in rank order. */
export function partitionByRank<T>(
  items: T[],
  rank: (t: T) => 0 | 1 | 2,
  sortKey?: (t: T) => string,
): { running: T[]; pending: T[]; finished: T[]; flat: T[] } {
  const running: T[] = [];
  const pending: T[] = [];
  const finished: T[] = [];
  for (const it of items) {
    (rank(it) === 0 ? running : rank(it) === 1 ? pending : finished).push(it);
  }
  if (sortKey) {
    const by = (a: T, b: T) => {
      const an = (sortKey(a) ?? "").toLowerCase();
      const bn = (sortKey(b) ?? "").toLowerCase();
      return an < bn ? -1 : an > bn ? 1 : 0;
    };
    running.sort(by);
    pending.sort(by);
    finished.sort(by);
  }
  return { running, pending, finished, flat: [...running, ...pending, ...finished] };
}
