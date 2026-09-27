import { describe, expect, test } from "bun:test";
import { resolveInlineContextKey } from "./inlineContext.js";

describe("runtime inline paging", () => {
  test("routes page keys through the central overlay binding resolver", () => {
    expect(resolveInlineContextKey("", { pageUp: true })).toEqual([
      { type: "page", delta: -1 },
    ]);
    expect(resolveInlineContextKey("", { pageDown: true })).toEqual([
      { type: "page", delta: 1 },
    ]);
  });
});
