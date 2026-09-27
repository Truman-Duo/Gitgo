import {expect, test} from "bun:test";
import {alignHunk} from "./DiffView.js";
import type {DiffHunk} from "../types.js";
import {canUseSplitDiff, diffAvail, splitColWidth} from "../theme/diffLayout.js";

test("split diff aligns contiguous remove/add blocks instead of only adjacent pairs", () => {
  const hunk: DiffHunk = {
    oldStart: 10, oldLines: 2, newStart: 20, newLines: 2,
    lines: [
      {type: "remove", text: "old a"},
      {type: "remove", text: "old b"},
      {type: "add", text: "new a"},
      {type: "add", text: "new b"},
    ],
  };
  expect(alignHunk(hunk)).toEqual([
    {oldLine: 10, oldText: "old a", newLine: 20, newText: "new a", type: "change"},
    {oldLine: 11, oldText: "old b", newLine: 21, newText: "new b", type: "change"},
  ]);
});

test("diff layout falls back before fixed gutters can squeeze three digit line numbers", () => {
  const narrow = diffAvail(48);
  expect(splitColWidth(narrow, 3)).toBeLessThan(18);
  expect(canUseSplitDiff(narrow, 3)).toBe(false);
  expect(canUseSplitDiff(diffAvail(100), 3)).toBe(true);
});

test("split diff preserves unequal block tails as additions or removals", () => {
  const hunk: DiffHunk = {
    oldStart: 1, oldLines: 1, newStart: 7, newLines: 2,
    lines: [
      {type: "remove", text: "old"},
      {type: "add", text: "new"},
      {type: "add", text: "extra"},
    ],
  };
  expect(alignHunk(hunk)).toEqual([
    {oldLine: 1, oldText: "old", newLine: 7, newText: "new", type: "change"},
    {oldLine: null, oldText: null, newLine: 8, newText: "extra", type: "add"},
  ]);
});
