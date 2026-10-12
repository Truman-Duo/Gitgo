import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { resolveInlineContextKey } from "./inlineContext.js";

describe("runtime inline paging", () => {
  test("routes page keys through the central overlay binding resolver", () => {
    assert.deepStrictEqual(resolveInlineContextKey("", { pageUp: true }), [
      { type: "page", delta: -1 },
    ]);
    assert.deepStrictEqual(resolveInlineContextKey("", { pageDown: true }), [
      { type: "page", delta: 1 },
    ]);
  });
});
