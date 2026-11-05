import {expect, test} from "bun:test";
import {configureTerminalWidths} from "../../vendor/ink/src/core/terminalWidth.js";
import {stringWidth} from "../../vendor/ink/src/core/stringWidth.js";
import {lineWidth} from "../../vendor/ink/src/core/line-width-cache.js";
import {displayWidth, truncate} from "./typography.js";
import wrapText from "../../vendor/ink/src/core/wrap-text.js";
import stripAnsi from "strip-ansi";

test("observed classic console widths agree across layout, cached lines and truncation", () => {
  try {
    configureTerminalWidths(undefined);
    expect(lineWidth("A·→中─")).toBe(6);
    configureTerminalWidths({183: 2, 8594: 2});
    expect(stringWidth("A·→中─")).toBe(8);
    expect(lineWidth("A·→中─")).toBe(8);
    expect(displayWidth("A·→中─")).toBe(8);
    expect(truncate("A·→中─", 4)).toBe("A·…");
    expect(stringWidth("─".repeat(80))).toBe(80);
    expect(stringWidth("e\u0301 🚀")).toBe(4);
    configureTerminalWidths({183: 2, 8594: 2, 8230: 2});
    expect(truncate("A·→中─", 4)).toBe("A…");
    expect(displayWidth(truncate("A·→中─", 4))).toBeLessThanOrEqual(4);
    expect(truncate("中中", 1)).toBe("");
    for (const mode of ["truncate", "truncate-start", "truncate-middle"] as const) {
      expect(stringWidth(wrapText("A·→中─", 4, mode))).toBeLessThanOrEqual(4);
    }
    const lines = wrapText("\x1b[31mA·→中─\x1b[39m", 4, "wrap").split("\n");
    expect(lines.every(line => stringWidth(line) <= 4)).toBe(true);
    expect(stripAnsi(lines.join(""))).toBe("A·→中─");
    configureTerminalWidths(undefined);
    expect(lineWidth("A·→中─")).toBe(6);
  } finally { configureTerminalWidths(undefined); }
});

test("terminal metrics cannot redefine ASCII, controls, or inject invalid widths", () => {
  try {
    configureTerminalWidths({65: 2, 10: 2, 183: 99, "8594suffix": 2});
    expect(stringWidth("A·→")).toBe(3);
  } finally { configureTerminalWidths(undefined); }
});
