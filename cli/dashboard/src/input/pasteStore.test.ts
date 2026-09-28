import { expect, test } from "bun:test";
import { PromptPasteStore } from "./pasteStore.js";

test("large pasted prompts collapse for editing and expand exactly before submission", () => {
  const store = new PromptPasteStore();
  const source = "一行\n二行\n三行";
  const display = store.collapse(source, true);
  expect(display).toBe("[Pasted text #1 +3 lines]");
  expect(store.materialize(`before ${display} after`)).toBe(`before ${source} after`);
});

test("ordinary typed and short pasted input remains directly editable", () => {
  const store = new PromptPasteStore();
  expect(store.collapse("hello", false)).toBe("hello");
  expect(store.collapse("hello", true)).toBe("hello");
});
