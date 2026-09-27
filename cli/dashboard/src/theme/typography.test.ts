import { describe, expect, test } from "bun:test";
import { contextPct, displayWidth, formatDuration, padEndWidth, truncate, wrap } from "./typography.js";

describe("terminal-cell typography", () => {
  test("measures CJK, combining marks and emoji graphemes", () => {
    expect(displayWidth("gitgo治理")).toBe(9);
    expect(displayWidth("e\u0301")).toBe(1);
    expect(displayWidth("👩‍💻")).toBe(2);
  });

  test("wraps and truncates by terminal cells without splitting graphemes", () => {
    expect(wrap("治理系统", 4)).toEqual(["治理", "系统"]);
    expect(truncate("项目治理", 5)).toBe("项目…");
    expect(displayWidth(padEndWidth("治理", 6))).toBe(6);
  });
});

describe("runtime duration", () => {
  test("rounds upward and omits zero-valued larger units", () => {
    expect(formatDuration(1)).toBe("0.01s");
    expect(formatDuration(10_001)).toBe("10.01s");
    expect(formatDuration(60_001)).toBe("1min,1s");
    expect(formatDuration(3_600_001)).toBe("1hour,1s");
    expect(formatDuration(3_723_000)).toBe("1hour,2min,3s");
  });
});

describe("context utilization", () => {
  test("keeps small persisted conversations visible without inventing a limit", () => {
    expect(contextPct(0, 128_000)).toBe("0%");
    expect(contextPct(64, 128_000)).toBe("0.1%");
    expect(contextPct(1_024, 128_000)).toBe("0.8%");
    expect(contextPct(20_000, 128_000)).toBe("16%");
    expect(contextPct(20_000)).toBe("?%");
  });
});
