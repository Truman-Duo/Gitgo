/** Lane allocation follows gitgraph-tui's parallel-until-parent invariant (MIT).
 * Copyright (c) 2026 gitgraph-tui contributors; vendor/oil-git/GGRAPH-LICENSE.
 * Gitgo owns the horizontal routing, page state and compatibility rendering.
 */
import {point, railCrossings, type Rail} from "./publishGraphRoutes.js";

export const COMMIT_SPACING = 80, LANE_SPACING = 28, LANE_MARGIN = 18;
export type GraphCommit = {oid: string; parents: string[]};
type Slot = {awaiting: string; colorIdx: number};
export type GraphState = {version?: 1; lanes: (Slot | null)[]; nextColor: number};
/** Read both previous presentation formats; no product/business state migration. */
export type GraphFrontier = (string | null)[] | GraphState;
export type GraphNode = {oid: string; column: number; lane: number; color: number; parents: {oid: string; lane: number}[]};
export type CommitGraph = {nodes: GraphNode[]; rails: Rail[]; incoming: GraphState; frontier: GraphState; lanes: number; head: string};

export function layoutCommitGraph(commits: GraphCommit[], pending: GraphFrontier = [], head = commits[0]?.oid ?? ""): CommitGraph {
  const state: GraphState = Array.isArray(pending)
    ? {version: 1, lanes: pending.map((awaiting, colorIdx) => awaiting ? {awaiting, colorIdx} : null), nextColor: pending.length}
    : {version: 1, lanes: pending.lanes.map(slot => slot ? {...slot} : null), nextColor: pending.nextColor};
  const incoming = structuredClone(state), lanes = state.lanes, nodes: GraphNode[] = [], rails: Rail[] = [];
  let width = lanes.length;
  const allocate = (awaiting: string) => {
    let lane = lanes.indexOf(null);
    if (lane < 0) lane = lanes.length;
    lanes[lane] = {awaiting, colorIdx: state.nextColor++};
    return lane;
  };
  const y = (lane: number) => LANE_MARGIN + lane * LANE_SPACING;
  for (const [column, commit] of commits.entries()) {
    const owners = lanes.flatMap((slot, lane) => slot?.awaiting === commit.oid ? [lane] : []);
    const lane = owners[0] ?? allocate(commit.oid), color = lanes[lane]!.colorIdx;
    const left = column * COMMIT_SPACING, center = left + COMMIT_SPACING / 2, right = left + COMMIT_SPACING;
    const add = (id: string, color: number, coordinates: [number, number][]) =>
      rails.push({id: `${column}/${id}`, color, points: coordinates.map(([x, lane]) => point(x, y(lane)))});
    // Existing tracks pass through unchanged; a shared awaited hash does not join early.
    for (const [other, slot] of lanes.entries()) if (slot && !owners.includes(other) && other !== lane)
      add(`pass/${other}`, slot.colorIdx, [[left, other], [right, other]]);
    if (owners.length) add("in", color, [[left, lane], [center, lane]]);
    const joins = owners.filter(other => other !== lane);
    for (const [index, other] of joins.entries()) {
      const bend = left + 8 + index * (COMMIT_SPACING / 2 - 16) / Math.max(1, joins.length);
      add(`join/${other}`, lanes[other]!.colorIdx, [[left, other], [bend, other], [bend, lane], [center, lane]]);
      lanes[other] = null;
    }
    const parents = [...new Set(commit.parents)], departures: {oid: string; lane: number}[] = [];
    if (!parents.length) lanes[lane] = null;
    else {
      lanes[lane] = {awaiting: parents[0]!, colorIdx: color};
      departures.push({oid: parents[0]!, lane});
      add("out", color, [[center, lane], [right, lane]]);
      for (const [index, parent] of parents.slice(1).entries()) {
        // Allocate even if another track awaits this parent. Merge only at its actual node.
        const fork = allocate(parent), bend = center + 12 + index * (COMMIT_SPACING / 2 - 20) / Math.max(1, parents.length - 1);
        departures.push({oid: parent, lane: fork});
        add(`fork/${fork}`, lanes[fork]!.colorIdx, [[center, lane], [bend, lane], [bend, fork], [right, fork]]);
      }
    }
    nodes.push({oid: commit.oid, column, lane, color, parents: departures});
    width = Math.max(width, lanes.length, lane + 1, ...owners.map(owner => owner + 1));
    while (lanes.length && !lanes.at(-1)) lanes.pop();
  }
  // Dashed stubs signify unloaded parents only at the page boundary; loaded rails stay solid.
  const boundary = commits.length * COMMIT_SPACING;
  for (const [lane, slot] of lanes.entries()) if (slot)
    rails.push({id: `pending/${lane}`, color: slot.colorIdx, pending: true,
      points: [point(boundary, y(lane)), point(boundary + COMMIT_SPACING / 3, y(lane))]});
  return {nodes, rails, incoming, frontier: state, lanes: width, head};
}

/** Compatibility view consumes the same normalized rails as SVG. Crossings are gaps, never '+'. */
export function drawCommitGraph(graph: CommitGraph, selected: number, width: number, height: number): string[] {
  if (!graph.nodes.length || width < 5 || height < 1) return [];
  const cell = 12, columns = Math.max(1, Math.floor((width - 4) / cell));
  const start = Math.max(0, Math.min(selected - Math.floor(columns / 2), graph.nodes.length - columns));
  const count = Math.min(columns, graph.nodes.length - start), end = start + count;
  const visibleLanes = Math.max(1, Math.floor(height / 3)), selectedLane = graph.nodes[selected]?.lane ?? 0;
  const topLane = Math.max(0, Math.min(selectedLane - Math.floor(visibleLanes / 2), graph.lanes - visibleLanes));
  const rows = Math.min(graph.lanes - topLane, visibleLanes) * 3, canvasWidth = Math.min(width - 3, count * cell);
  const canvas = Array.from({length: rows}, () => Array.from({length: canvasWidth}, () => " "));
  const links = Array.from({length: rows}, () => new Uint8Array(canvasWidth));
  const mark = (x: number, y: number, char: string) => {
    if (x >= 0 && x < canvasWidth && y >= 0 && y < rows) canvas[y]![x] = char;
  };
  const x = (value: number) => Math.round((value / COMMIT_SPACING - start) * cell);
  const y = (value: number) => Math.round(((value - LANE_MARGIN) / LANE_SPACING - topLane) * 3);
  const link = (col: number, row: number, direction: number) => {
    if (col >= 0 && col < canvasWidth && row >= 0 && row < rows) links[row]![col] |= direction;
  };
  for (const rail of graph.rails) for (let i = 1; i < rail.points.length; i++) {
    const a = rail.points[i - 1]!, b = rail.points[i]!;
    if (a.y === b.y) {
      const first = Math.min(x(a.x), x(b.x)), last = Math.max(x(a.x), x(b.x));
      for (let col = Math.max(0, first); col <= Math.min(canvasWidth - 1, last); col++) link(col, y(a.y), (col > first ? 8 : 0) | (col < last ? 2 : 0));
    } else {
      const first = Math.min(y(a.y), y(b.y)), last = Math.max(y(a.y), y(b.y));
      for (let row = Math.max(0, first); row <= Math.min(rows - 1, last); row++) link(x(a.x), row, (row > first ? 1 : 0) | (row < last ? 4 : 0));
    }
  }
  const glyphs = [" ","│","─","└","│","│","┌","├","─","┘","─","┴","┐","┤","┬","┼"];
  for (let row = 0; row < rows; row++) for (let col = 0; col < canvasWidth; col++) mark(col, row, glyphs[links[row]![col]!]!);
  for (const cross of railCrossings(graph.rails)) {mark(x(cross.x) - 1, y(cross.y), " "); mark(x(cross.x) + 1, y(cross.y), " "); mark(x(cross.x), y(cross.y), "│");}
  if (end === graph.nodes.length) for (const [lane, slot] of graph.frontier.lanes.entries()) if (slot) mark(canvasWidth - 1, (lane - topLane) * 3, ">");
  for (const node of graph.nodes.slice(start, end)) {
    const col = x((node.column + .5) * COMMIT_SPACING), row = (node.lane - topLane) * 3;
    mark(col, row, node.column === selected ? "O" : "*");
    const label = node.oid.slice(0, 8), labelX = Math.max(0, Math.min(col, canvasWidth - label.length));
    if (row >= 0 && canvas[row + 1]) for (let i = 0; i < label.length; i++) mark(labelX + i, row + 1, label[i]!);
  }
  return canvas.map((row, index) => `${String(topLane + Math.floor(index / 3) + 1).padStart(2, "0")} ${row.join("").trimEnd()}`);
}
