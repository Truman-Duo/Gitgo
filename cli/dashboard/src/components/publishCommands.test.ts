import {test, expect} from "bun:test";
import {publishCommands, publishSuggestions} from "./publishCommands.js";
const t = (copy: {en: string; zh: string}) => copy.zh;
test("command discovery follows the selected item's actual allowed actions", () => {
  const item: any = {identity_kind: "stable_item", allowed_actions: ["verify", "collaborate"]};
  const names = publishCommands({item}).map(row => row.name);
  expect(names).toContain("verify"); expect(names).toContain("history"); expect(names).toContain("comment");
  expect(names).not.toContain("execute"); expect(names).not.toContain("task");
  expect(publishSuggestions({item}, "/comment", t)[0]?.label).toBe("/comment");
  item.allowed_actions = ["execute"];
  expect(publishSuggestions({item}, "/ver", t)).toEqual([]);
  expect(publishSuggestions({item}, "/exe", t)[0]?.label).toBe("/execute");
});
test("empty, running and paginated contexts expose only applicable controls", () => {
  expect(publishCommands({}).map(row => row.name)).not.toContain("history");
  expect(publishCommands({busy: true}).map(row => row.name)).toEqual(["help","refresh", "pause"]);
  expect(publishCommands({more: true, retry: true}).map(row => row.name)).toEqual(expect.arrayContaining(["more", "retry"]));
  expect(publishSuggestions({}, "", t)).toEqual([]);
});
