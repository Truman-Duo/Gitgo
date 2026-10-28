import {expect, test} from "bun:test";
import {parseContributions, parseSelection} from "./publishSelection.js";

test("source roles, fixed bases and ordered picks survive draft parsing", () => {
  expect(parseContributions(`trial | refs/heads/one | ${"a".repeat(40)}; workspace | HEAD`)).toEqual([
    {source_role: "trial", source_ref: "refs/heads/one", base_oid: "a".repeat(40)}, {source_role: "workspace", source_ref: "HEAD"},
  ]);
  expect(parseSelection("cherry_pick", `${"a".repeat(40)} ${"b".repeat(40)}`, "")).toEqual({selected_commits: ["a".repeat(40), "b".repeat(40)]});
  expect(parseSelection("partial", "", "src/a file.py; tests/test.py")).toEqual({paths: ["src/a file.py", "tests/test.py"]});
});

test("draft selections reject ambiguous roles, abbreviated IDs and escaping paths", () => {
  for (const value of ["remote | main", "trial", "trial | HEAD | abc"]) expect(() => parseContributions(value)).toThrow();
  expect(() => parseSelection("cherry_pick", "abc1234", "")).toThrow();
  expect(() => parseSelection("cherry_pick", `${"a".repeat(40)} ${"a".repeat(40)}`, "")).toThrow();
  for (const value of ["../secret", "C:\\secret", "src/../secret", "/absolute"]) expect(() => parseSelection("partial", "", value)).toThrow();
});
