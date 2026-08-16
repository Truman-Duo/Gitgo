/**
 * Minimal reproduction: main-screen terminal resize duplicate rendering.
 * No Ink, no React — pure ANSI escape sequences via bun.
 *
 * Usage: bun run repro-resize.ts
 * Then resize the terminal window and observe whether old content persists.
 *
 * Key insight: Bun on ConPTY/Windows may not fire "resize" events unless
 * stdin is in raw mode. This script sets raw mode (matching Ink's setup)
 * AND polls stdout.columns/rows as a fallback.
 */
import { stdin, stdout } from "node:process";

// ── ANSI sequences ──
const CURSOR_HOME = "\x1b[H";
const ED_SCREEN = "\x1b[2J";

// ── Terminal size ──
function cols(): number { return stdout.columns || 80; }
function rows(): number { return stdout.rows || 24; }

// ── Fill screen and redraw ──
let resizeCount = 0;
function redraw(): void {
  const w = cols();
  const h = rows();
  const char = String.fromCharCode(65 + (resizeCount % 26)); // A, B, C...
  // Write status line as first row so it's always visible
  const statusLine = `[repro#${resizeCount}] cols=${w} rows=${h} fill='${char}'`;
  const pad = Math.max(0, w - statusLine.length);
  const line = char.repeat(w);

  stdout.write(
    ED_SCREEN + CURSOR_HOME +
    statusLine + " ".repeat(pad) + "\r\n" +  // status as row 1
    (line + "\r\n").repeat(h - 1) +           // fill remaining rows
    CURSOR_HOME                                // park cursor
  );
}

// ── Resize detection: both event AND polling ──
process.stdout.on("resize", () => {
  resizeCount++;
  redraw();
});

let prevCols = cols();
let prevRows = rows();
setInterval(() => {
  const c = cols();
  const r = rows();
  if (c !== prevCols || r !== prevRows) {
    prevCols = c;
    prevRows = r;
    resizeCount++;
    redraw();
  }
}, 100);

// ── Set raw mode (required for resize events on ConPTY) ──
if (stdin.isTTY && stdin.setRawMode) {
  stdin.setRawMode(true);
}
// Prevent process from exiting
setInterval(() => {}, 1000);

// ── Initial draw ──
redraw();
