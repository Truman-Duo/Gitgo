// src/theme/typography.ts — Pure-function text utilities.

import type { StatusState, StatusDot } from "./types.js";
import { colors } from "./tokens.js";
import { eastAsianWidth } from "get-east-asian-width";
import type { HexColor } from "@anthropic/ink";

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

function graphemes(text: string): string[] {
  return Array.from(graphemeSegmenter.segment(text), (part) => part.segment);
}

function graphemeWidth(value: string): number {
  if (value === "\t") return 4;
  let width = 0;
  for (const char of value) {
    const point = char.codePointAt(0) ?? 0;
    if (point === 0x200d || /\p{Mark}/u.test(char) || point === 0xfe0f) continue;
    width = Math.max(width, eastAsianWidth(point));
  }
  return width;
}

export function displayWidth(text: string): number {
  return graphemes(text).reduce((total, item) => total + graphemeWidth(item), 0);
}

/** Truncate string with ellipsis. */
export function truncate(s: string, max: number): string {
  if (displayWidth(s) <= max) return s;
  const target = Math.max(0, max - 1);
  let width = 0;
  let result = "";
  for (const item of graphemes(s)) {
    const next = graphemeWidth(item);
    if (width + next > target) break;
    result += item;
    width += next;
  }
  return result + "…";
}

export function padEndWidth(s: string, width: number): string {
  return s + " ".repeat(Math.max(0, width - displayWidth(s)));
}

/** Terminal-cell-aware wrapper for CJK, combining marks and emoji graphemes. */
export function wrap(text: string, maxW: number): string[] {
  if (!text) return [""];
  const lines: string[] = [];
  for (const para of text.split("\n")) {
    if (!para) { lines.push(""); continue; }
    let current = "";
    let width = 0;
    for (const item of graphemes(para)) {
      const next = graphemeWidth(item);
      if (current && width + next > maxW) {
        lines.push(current);
        current = "";
        width = 0;
      }
      current += item;
      width += next;
    }
    if (current) lines.push(current);
  }
  return lines;
}

/** Linear interpolate between two hex colors. */
export function lerpColor(a: HexColor, b: HexColor, t: number): HexColor {
  const parseHex = (s: string) => [1, 3, 5].map((i) => parseInt(s.slice(i, i + 2), 16));
  const [r1, g1, b1] = parseHex(a);
  const [r2, g2, b2] = parseHex(b);
  const lerp = (x: number, y: number) => Math.round(x + (y - x) * t);
  return (
    "#" +
    [lerp(r1, r2), lerp(g1, g2), lerp(b1, b2)]
      .map((v) => v.toString(16).padStart(2, "0"))
      .join("")
  ) as HexColor;
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

/** Context utilization from the process/provider-specific limit; never invent 128K. */
export function contextPct(estimatedTokens: number, contextWindow?: number): string {
  if (!estimatedTokens || estimatedTokens <= 0) return "0%";
  if (!contextWindow || contextWindow <= 0) {
    return "?%";
  }
  const raw = (estimatedTokens / contextWindow) * 100;
  // Preserve meaningful low utilization instead of rounding every small but
  // growing conversation to the indistinguishable value 0%.
  const pct = raw < 10 ? Math.ceil(raw * 10) / 10 : Math.round(raw);
  return `${pct}%`;
}

/** Human duration used by live and persisted runtime projections.
 * Under one minute the display has 0.01 s resolution and rounds upward so a
 * completed operation never appears faster than it was. Larger units omit
 * zero-valued slots: 1min,1s and 1hour,2min,3s. */
export function formatDuration(milliseconds: number): string {
  const safeMs = Math.max(0, Number(milliseconds) || 0);
  if (safeMs < 60_000) {
    const centiseconds = Math.ceil(safeMs / 10);
    return `${(centiseconds / 100).toFixed(2)}s`;
  }
  const totalSeconds = Math.ceil(safeMs / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}hour`);
  if (minutes > 0) parts.push(`${minutes}min`);
  if (seconds > 0) parts.push(`${seconds}s`);
  return parts.join(",");
}

/** Map a process status string to its StatusState for statusDot(). */
export function processStatusToDot(s: string): StatusState {
  if (s === "running") return "ok";
  if (s === "completed" || s === "recovered") return "done";
  if ([
    "waiting", "awaiting_user", "cancelling", "recovering", "resume_available",
  ].includes(s)) return "warning";
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
