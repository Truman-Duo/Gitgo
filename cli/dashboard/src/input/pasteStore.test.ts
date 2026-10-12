import { test } from "node:test";
import assert from "node:assert/strict";
import { PromptPasteStore } from "./pasteStore.js";

test("large pasted prompts collapse for editing and expand exactly before submission", () => {
  const store = new PromptPasteStore();
  const source = "一行\n二行\n三行";
  const display = store.collapse(source, true);
  assert.strictEqual(display, "[Pasted text #1 +3 lines]");
  assert.strictEqual(store.materialize(`before ${display} after`), `before ${source} after`);
});

test("ordinary typed and short pasted input remains directly editable", () => {
  const store = new PromptPasteStore();
  assert.strictEqual(store.collapse("hello", false), "hello");
  assert.strictEqual(store.collapse("hello", true), "hello");
});
