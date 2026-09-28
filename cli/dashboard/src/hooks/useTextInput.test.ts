import {describe, expect, test} from "bun:test";
import {visualLineCursor} from "./useTextInput.js";

describe("visual-line prompt navigation", () => {
  test("moves between terminal-wrapped lines while retaining the column", () => {
    const value = "0123456789abcdefghij";
    expect(visualLineCursor(value, 16, -1, 10)).toBe(6);
    expect(visualLineCursor(value, 6, 1, 10)).toBe(16);
  });

  test("uses terminal cell width for CJK text", () => {
    const value = "甲乙丙丁戊己";
    expect(visualLineCursor(value, 5, -1, 6)).toBe(2);
  });

  test("treats Windows CRLF as one visual line boundary", () => {
    const value = "first\r\nsecond\r\nthird";
    expect(visualLineCursor(value, value.length, -1, 20)).toBe(12);
    expect(visualLineCursor(value, 13, -1, 20)).toBe(5);
  });
});
