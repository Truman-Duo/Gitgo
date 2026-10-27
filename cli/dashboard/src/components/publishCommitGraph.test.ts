import {test, expect} from "bun:test";
import {layoutCommitGraph, drawCommitGraph} from "./publishCommitGraph.js";

test("a real diamond has separate stable branch lanes and rejoins its common parent", () => {
  const commits = [{oid: "merge", parents: ["left", "right"]}, {oid: "left", parents: ["root"]},
    {oid: "right", parents: ["root"]}, {oid: "root", parents: []}];
  const graph = layoutCommitGraph(commits);
  expect(graph.nodes.map(row => row.lane)).toEqual([0, 0, 1, 0]);
  expect(graph.nodes.flatMap(row => row.parents.map(parent => [row.oid, parent.oid]))).toEqual([
    ["merge", "left"], ["merge", "right"], ["left", "root"], ["right", "root"]]);
  expect(graph.frontier.filter(Boolean)).toEqual([]);
  const text = drawCommitGraph(graph, 2, 80, 15).join("\n");
  expect(text).toContain("O"); expect(text).toContain("+"); expect(text).toContain("02");
});

test("pagination preserves pending lanes instead of inventing edges or resetting branches", () => {
  const first = layoutCommitGraph([{oid: "merge", parents: ["left", "right"]}]);
  const second = layoutCommitGraph([{oid: "left", parents: ["root"]}, {oid: "right", parents: ["root"]}], first.frontier);
  expect(second.nodes.map(row => row.lane)).toEqual([0, 1]);
  const last = layoutCommitGraph([{oid: "root", parents: []}], second.frontier);
  expect(last.nodes[0]!.lane).toBe(0); expect(last.nodes[0]!.parents).toEqual([]);
  expect(drawCommitGraph(second, 1, 40, 6).every(line => line.length <= 40)).toBe(true);
});

test("octopus merges, multiple roots and a narrow clipped viewport retain real parent identity", () => {
  const graph = layoutCommitGraph([{oid: "merge", parents: ["a", "b", "c"]}, {oid: "a", parents: []}, {oid: "b", parents: []}, {oid: "c", parents: []}, {oid: "unrelated", parents: []}]);
  expect(graph.nodes[0]!.parents.map(row => row.lane)).toEqual([0, 1, 2]);
  expect(graph.nodes[4]!.parents).toEqual([]);
  const clipped = drawCommitGraph(graph, 3, 20, 3);
  expect(clipped).toHaveLength(3); expect(clipped.join("\n")).toContain("O");
});
