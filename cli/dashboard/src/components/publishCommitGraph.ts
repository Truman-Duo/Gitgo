export type GraphCommit = {oid: string; parents: string[]};
export type GraphNode = {oid: string; column: number; lane: number; parents: {oid: string; lane: number}[]};
export type CommitGraph = {nodes: GraphNode[]; incoming: (string | null)[]; frontier: (string | null)[]; lanes: number};

/** Traverse actual Git parent edges. Occupied lanes never move, including at a page boundary. */
export function layoutCommitGraph(commits: GraphCommit[], incoming: (string | null)[] = []): CommitGraph {
  const lanes = [...incoming], nodes: GraphNode[] = [];
  let maximum = lanes.length;
  const allocate = (oid: string) => {
    const known = lanes.indexOf(oid);
    if (known >= 0) return known;
    let lane = lanes.indexOf(null);
    if (lane < 0) lane = lanes.length;
    lanes[lane] = oid; maximum = Math.max(maximum, lanes.length);
    return lane;
  };
  commits.forEach((commit, column) => {
    const lane = allocate(commit.oid);
    lanes[lane] = null;
    const parents = commit.parents.map((oid, index) => {
      // Preserve first-parent continuity unless that parent already owns a lane.
      if (index === 0 && !lanes.includes(oid)) lanes[lane] = oid;
      return {oid, lane: allocate(oid)};
    });
    nodes.push({oid: commit.oid, lane, column, parents});
  });
  return {nodes, incoming: [...incoming], frontier: lanes, lanes: maximum};
}

/** Horizontal time axis: newer commits on the left, real parents to the right. */
export function drawCommitGraph(graph: CommitGraph, selected: number, width: number, height: number): string[] {
  const cell = 12, columns = Math.max(1, Math.floor((width - 4) / cell));
  const start = Math.max(0, Math.min(selected - Math.floor(columns / 2), graph.nodes.length - columns));
  const count = Math.min(columns, graph.nodes.length - start), end = start + count;
  const visibleLanes = Math.max(1, Math.floor(height / 3));
  const selectedLane = graph.nodes[selected]?.lane ?? 0;
  const topLane = Math.max(0, Math.min(selectedLane - Math.floor(visibleLanes / 2), graph.lanes - visibleLanes));
  const rows = Math.min(graph.lanes - topLane, visibleLanes) * 3;
  const canvas = Array.from({length: rows}, () => Array.from({length: Math.max(1, count * cell)}, () => " "));
  const mark = (x: number, y: number, char: string) => {
    if (x < 0 || x >= count * cell || y < 0 || y >= rows) return;
    const old = canvas[y]![x];
    canvas[y]![x] = old === " " || old === char ? char : "+";
  };
  const edge = (column: number, lane: number, parentColumn: number, parentLane: number) => {
    const x = (column - start) * cell + 1, target = (parentColumn - start) * cell + 1;
    const y = (lane - topLane) * 3, py = (parentLane - topLane) * 3;
    const junction = x + cell - 2;
    for (let cx = Math.max(0, x); cx <= Math.min(canvas[0]?.length ?? 0, junction); cx++) mark(cx, y, "-");
    if (lane !== parentLane) for (let cy = Math.max(0, Math.min(y, py)); cy <= Math.min(rows - 1, Math.max(y, py)); cy++) mark(junction, cy, "|");
    for (let cx = Math.max(0, junction); cx <= Math.min((canvas[0]?.length ?? 1) - 1, target); cx++) mark(cx, py, "-");
    if (target >= count * cell) mark(count * cell - 1, py, ">");
  };
  const positions = new Map(graph.nodes.map(node => [node.oid, node.column]));
  graph.incoming.forEach((oid, lane) => {
    if (oid) edge(-1, lane, positions.get(oid) ?? graph.nodes.length, lane);
  });
  for (const node of graph.nodes) for (const parent of node.parents) {
    const target = positions.get(parent.oid) ?? graph.nodes.length;
    if (node.column < end && target >= start) edge(node.column, node.lane, target, parent.lane);
  }
  for (const node of graph.nodes.slice(start, end)) {
    const x = (node.column - start) * cell + 1, y = (node.lane - topLane) * 3;
    if (y < 0 || y >= rows) continue;
    canvas[y]![x] = node.column === selected ? "O" : "*";
    const label = node.oid.slice(0, 8);
    if (canvas[y + 1]) for (let i = 0; i < label.length; i++) canvas[y + 1]![x + i] = label[i]!;
  }
  return canvas.map((row, index) => `${String(topLane + Math.floor(index / 3) + 1).padStart(2, "0")} ${row.join("").trimEnd()}`);
}
