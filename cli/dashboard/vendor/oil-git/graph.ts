/** Ported from oil-oil/oil-git 9e6abfabe63503b5122b1762a8902ecefe9473fc, src/graph.ts.
 * Copyright (c) 2026 Zhihuang Lin (oil-oil). MIT; see ./LICENSE.
 * Changes: minimal input type and pending lane input/output for fixed-snapshot pages.
 */
export type Commit = {hash: string; parents: string[]};
export const ROW_HEIGHT = 56;
export const COLORS = Array.from(
  { length: 6 },
  (_, i) => "var(--lane-" + i + ")",
);
type Point = { x: number; y: number; lane: number; index: number };
type Edge = {
  from: Point;
  to: Point;
  fromHash: string;
  toHash: string;
  pending: boolean;
  color: string;
};
function indexEdges(edges: Edge[], count: number) {
  let base = 1;
  while (base < count) base *= 2;
  const groups: Edge[][] = Array.from({ length: count }, () => []);
  const maxEnd = new Int32Array(base * 2).fill(-1);
  for (const edge of edges) {
    groups[edge.from.index].push(edge);
    const leaf = base + edge.from.index;
    maxEnd[leaf] = Math.max(maxEnd[leaf], edge.to.index);
  }
  for (let i = base - 1; i > 0; i--)
    maxEnd[i] = Math.max(maxEnd[i * 2], maxEnd[i * 2 + 1]);
  return { groups, maxEnd, base };
}
export function layoutGraph(commits: Commit[], head: string | null, incoming?: (string | null)[]) {
  const lanes: (string | null)[] = incoming ? [...incoming] : head ? [head] : [];
  const positions = new Map<
    string,
    { x: number; y: number; lane: number; index: number }
  >();
  let max = 0;
  const reserve = (hash: string) => {
    let lane = lanes.indexOf(hash);
    if (lane < 0) {
      lane = lanes.indexOf(null);
      if (lane < 0) lane = lanes.length;
      lanes[lane] = hash;
    }
    return lane;
  };
  for (const [index, commit] of commits.entries()) {
    const lane = reserve(commit.hash);
    max = Math.max(max, lane);
    positions.set(commit.hash, {
      x: 24 + lane * 24,
      y: index * ROW_HEIGHT + ROW_HEIGHT / 2,
      lane,
      index,
    });
    lanes[lane] = null;
    commit.parents.forEach((parent, i) => {
      if (lanes.includes(parent)) return;
      if (i === 0 && lanes[lane] === null) lanes[lane] = parent;
      else reserve(parent);
    });
  }
  const continuations = lanes.flatMap((hash, lane) =>
    hash ? [{ hash, x: 24 + lane * 24 }] : [],
  );
  max = Math.max(max, ...continuations.map((c) => (c.x - 24) / 24));
  const edges = commits.flatMap((c) => {
    const from = positions.get(c.hash)!;
    return c.parents.map((hash) => ({
      from,
      to: positions.get(hash) ?? {
        x: continuations.find((p) => p.hash === hash)?.x ?? from.x,
        y: commits.length * ROW_HEIGHT + 16,
        index: commits.length,
        lane: from.lane,
      },
      fromHash: c.hash,
      toHash: hash,
      pending: !positions.has(hash),
      color: COLORS[from.lane % COLORS.length],
    }));
  });
  return {
    frontier: [...lanes],
    positions,
    edges,
    edgeIndex: indexEdges(edges, commits.length),
    width: Math.max(72, (max + 1) * 24 + 24),
  };
}
// 区间索引保留跨越视口的长连线，滚动时跳过完全在视口外的子树。
export function visibleEdges(
  graph: ReturnType<typeof layoutGraph>,
  start: number,
  end: number,
) {
  const { groups, maxEnd, base } = graph.edgeIndex;
  const found: Edge[] = [];
  const visit = (node: number, lo: number, hi: number) => {
    if (lo >= end || maxEnd[node] < start) return;
    if (hi - lo === 1) {
      for (const edge of groups[lo] ?? [])
        if (edge.to.index >= start) found.push(edge);
      return;
    }
    const mid = (lo + hi) / 2;
    visit(node * 2, lo, mid);
    visit(node * 2 + 1, mid, hi);
  };
  visit(1, 0, base);
  return found;
}
export function curve(
  from: { x: number; y: number },
  to: { x: number; y: number },
) {
  const bend = Math.min((to.y - from.y) * 0.45, 38);
  return (
    "M" +
    from.x +
    " " +
    from.y +
    " C" +
    from.x +
    " " +
    (from.y + bend) +
    "," +
    to.x +
    " " +
    (to.y - bend) +
    "," +
    to.x +
    " " +
    to.y
  );
}
