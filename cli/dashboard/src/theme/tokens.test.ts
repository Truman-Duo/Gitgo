import { expect, test } from "bun:test";
import { cacheHitColor, colors } from "./tokens.js";

test("cache dot thresholds have no gaps or overlapping bands", () => {
  for (const [value, expected] of [
    [100, colors.cache.green1], [95, colors.cache.green1],
    [94.99, colors.cache.green2], [90, colors.cache.green2],
    [89.99, colors.cache.brown1], [85, colors.cache.brown1],
    [84.99, colors.cache.brown2], [80, colors.cache.brown2],
    [79.99, colors.cache.yellow], [70, colors.cache.yellow],
    [69.99, colors.cache.orange], [50, colors.cache.orange],
    [49.99, colors.cache.red], [0, colors.cache.red],
  ] as const) expect(cacheHitColor(value)).toBe(expected);
  expect(cacheHitColor(null)).toBe(colors.cache.unknown);
  expect(cacheHitColor(NaN)).toBe(colors.cache.unknown);
});
