// Quick test: verify TextInput cursor position calculation for CJK text
import { stringWidth, wrapText } from "@anthropic/ink";

function computeCursor(value: string, cursor: number, maxWidth: number) {
  if (value.length === 0) return { line: 0, column: 0 };
  const effectiveWidth = maxWidth > 0 ? maxWidth : 80;
  const prefix = value.slice(0, cursor);
  const prefixWrapped = wrapText(prefix, effectiveWidth, "wrap");
  const prefixLines = prefixWrapped.split("\n");
  const cursorLine = prefixLines.length - 1;
  const cursorColumn = stringWidth(prefixLines[cursorLine]!);
  return { line: cursorLine, column: cursorColumn };
}

// Simulate typing CJK characters one at a time
function simulateTyping(chars: string[], maxWidth: number) {
  let value = "";
  console.log(`maxWidth=${maxWidth}, terminalWidth=80`);
  for (let i = 0; i < chars.length; i++) {
    value += chars[i];
    const cursor = value.length;
    const pos = computeCursor(value, cursor, maxWidth);
    const promptWidth = 2; // "▸ "
    const padding = 1;
    const absoluteX = padding + promptWidth + pos.column;
    const absoluteY = pos.line; // relative to Box, which is at same Y as text
    console.log(
      `  char #${String(i + 1).padStart(2)}: ${chars[i]}  value.length=${String(value.length).padStart(2)}  sw=${String(stringWidth(value)).padStart(2)}  line=${pos.line}  col=${pos.column}  absX=${absoluteX}  (terminal ${absoluteX > 80 ? "WRAPS!" : "ok"})`
    );
  }
}

// Test 1: Single-width CJK (U+4E00-U+9FFF)
console.log("\n=== Test 1: 30 CJK chars (你好) ===");
const cjk: string[] = [];
for (let i = 0; i < 30; i++) cjk.push(i % 2 === 0 ? "你" : "好");
simulateTyping(cjk, 77); // 80 - prompt(2) - padding(1) = 77

// Test 2: Mixed ASCII + CJK
console.log("\n=== Test 2: Mixed ASCII + CJK ===");
const mixed = "hello世界你好world".split("");
simulateTyping(mixed, 77);

// Test 3: Exactly at wrap boundary
console.log("\n=== Test 3: CJK chars at boundary (38 cells = 19 CJK, wrap at 40) ===");
const boundary: string[] = [];
for (let i = 0; i < 25; i++) boundary.push("测");
simulateTyping(boundary, 40); // narrow: wrap at 40

// Test 4: Full width CJK at wider terminal
console.log("\n=== Test 4: CJK at width 77 (80-col terminal, 3 col offset) ===");
const wider: string[] = [];
for (let i = 0; i < 45; i++) wider.push("字");
simulateTyping(wider, 77);
