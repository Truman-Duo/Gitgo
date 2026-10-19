import { expect, test } from "bun:test";
import { clampHeaderIndex, fitHeaderWindow } from "./HorizontalHeaderStrip.js";

test("header navigation clamps instead of wrapping", () => {
  expect(clampHeaderIndex(0, -1, 4)).toBe(0);
  expect(clampHeaderIndex(3, 1, 4)).toBe(3);
  expect(clampHeaderIndex(1, 1, 4)).toBe(2);
});

test("overflow reserves an ellipsis slot and keeps the selected tab visible", () => {
  const items = Array.from({length: 8}, (_, index) => ({id: String(index), label: `tab-${index}`}));
  const result = fitHeaderWindow(items, 5, 24);
  expect(result.start).toBeGreaterThan(0);
  expect(result.end).toBeLessThanOrEqual(items.length);
  expect(result.start).toBeLessThanOrEqual(5);
  expect(result.end).toBeGreaterThan(5);
  expect(result.left).toBe(true);
  expect(result.slotWidth).toBe(7);
});

test("ellipsis consumes the same fixed slot and edge navigation does not wrap", () => {
  const items = Array.from({length: 8}, (_, index) => ({id: String(index), label: `tab-${index}`}));
  const left = fitHeaderWindow(items, 0, 28);
  const right = fitHeaderWindow(items, 7, 28);
  expect(left.left).toBe(false);
  expect(left.right).toBe(true);
  expect(right.left).toBe(true);
  expect(right.right).toBe(false);
  expect(left.slotWidth).toBe(right.slotWidth);
});

test('very narrow headers retain the selected tab within their cell budget', () => {
  const items = Array.from({length: 8}, (_, index) => ({id: String(index), label: `tab-${index}`}));
  for (const width of [1, 6, 7, 10, 14, 20]) {
    for (let selected = 0; selected < items.length; selected++) {
      const result = fitHeaderWindow(items, selected, width);
      expect(result.start).toBeLessThanOrEqual(selected); expect(result.end).toBeGreaterThan(selected);
      const slots = result.end - result.start + Number(result.left) + Number(result.right);
      expect(slots * result.slotWidth).toBeLessThanOrEqual(width);
    }
  }
});
