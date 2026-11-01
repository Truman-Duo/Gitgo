/** Shared geometry for Git ancestry and business-role diagrams. Coordinates are SVG pixels. */
export type Point = {x: number; y: number};
export type Rail = {id: string; color: number; points: Point[]; pending?: boolean};
export type Crossing = {x: number; y: number; over: Rail; under: Rail};
export const point = (x: number, y: number): Point => ({x, y});

/** Orthogonal rails only; round local corners without bowing across other tracks. */
export function roundedRail(points: Point[], radius = 5): string {
  const clean = points.filter((p, i) => !i || p.x !== points[i - 1]!.x || p.y !== points[i - 1]!.y);
  if (!clean.length) return "";
  let path = `M${clean[0]!.x} ${clean[0]!.y}`;
  for (let i = 1; i < clean.length; i++) {
    const p = clean[i]!, before = clean[i - 1]!, after = clean[i + 1];
    if (!after || (before.x === p.x && after.x === p.x) || (before.y === p.y && after.y === p.y)) {
      path += `L${p.x} ${p.y}`;
      continue;
    }
    const distance = (a: Point, b: Point) => Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
    const r = Math.min(radius, distance(before, p) / 2, distance(p, after) / 2);
    const enter = point(p.x + Math.sign(before.x - p.x) * r, p.y + Math.sign(before.y - p.y) * r);
    const leave = point(p.x + Math.sign(after.x - p.x) * r, p.y + Math.sign(after.y - p.y) * r);
    path += `L${enter.x} ${enter.y}Q${p.x} ${p.y} ${leave.x} ${leave.y}`;
  }
  return path;
}

/** Only strict interior crossings get an overpass. Endpoints are real junctions. */
export function railCrossings(rails: Rail[], radius = 5): Crossing[] {
  const vertical: {rail: Rail; a: Point; b: Point}[] = [], horizontal: typeof vertical = [];
  for (const rail of rails) for (let i = 1; i < rail.points.length; i++) {
    const a = rail.points[i - 1]!, b = rail.points[i]!;
    if (a.x === b.x && a.y !== b.y) vertical.push({rail, a, b});
    else if (a.y === b.y && a.x !== b.x) horizontal.push({rail, a, b});
  }
  const result: Crossing[] = [], seen = new Set<string>();
  for (const v of vertical) for (const h of horizontal) {
    if (v.rail === h.rail) continue;
    const x = v.a.x, y = h.a.y;
    if (x <= Math.min(h.a.x, h.b.x) + radius || x >= Math.max(h.a.x, h.b.x) - radius ||
      y <= Math.min(v.a.y, v.b.y) + radius || y >= Math.max(v.a.y, v.b.y) - radius) continue;
    const key = `${x}/${y}`;
    if (!seen.has(key)) { seen.add(key); result.push({x, y, over: v.rail, under: h.rail}); }
  }
  return result;
}

export const ROLE_NAMES = ["workspace", "trial", "formal"] as const;
export type RoleEdge = {from: string; to: string};
export type RoleRoute = Rail & RoleEdge & {count: number; channel: number; arrow: Point[]};
export type RoleLayout = {routes: RoleRoute[]; nodes: {role: string; x: number; y: number; active: boolean; source: boolean; color: number}[]};

/** Stable role positions, one channel per directed pair, with reverse traffic below. */
export function layoutRoleRoutes(edges: RoleEdge[], width: number, height: number): RoleLayout {
  const order = (role: string) => ROLE_NAMES.indexOf(role as typeof ROLE_NAMES[number]);
  const grouped = new Map<string, RoleEdge & {count: number}>();
  for (const edge of edges) {
    if (order(edge.from) < 0 || order(edge.to) < 0) continue;
    const id = `${edge.from}/${edge.to}`, old = grouped.get(id);
    grouped.set(id, {...edge, count: (old?.count ?? 0) + 1});
  }
  const pairs = [...grouped.values()].sort((a, b) => Math.abs(order(a.to) - order(a.from)) - Math.abs(order(b.to) - order(b.from)) ||
    order(a.from) - order(b.from) || order(a.to) - order(b.to));
  const above = pairs.filter(edge => order(edge.from) <= order(edge.to)).length;
  const below = pairs.length - above;
  const baseline = height * .6;
  const topGap = Math.min(16, (baseline - 10) / (above + 1));
  const bottomGap = Math.min(16, (height - 10 - baseline) / (below + 1));
  const portStep = Math.max(1, Math.min(6, (width / 6 - 12) / Math.max(1, above, below)));
  const position = (role: string) => (order(role) + .5) * width / 3;
  let topChannel = 0, bottomChannel = 0;
  const routes: RoleRoute[] = pairs.map(edge => {
    const from = position(edge.from), to = position(edge.to), forward = to >= from;
    const channel = forward ? ++topChannel : ++bottomChannel;
    const level = baseline + (forward ? -channel * topGap : channel * bottomGap);
    const direction = Math.sign(to - from) || 1;
    // Distinct vertical ports prevent overlapping connectors at a shared role.
    // Outer channels use inner ports, so nested same-destination routes need no crossing.
    const port = 6 + ((forward ? above : below) - channel + 1) * portStep;
    const start = from === to ? from - port : from + direction * port;
    const finish = from === to ? to + port : to - direction * port;
    const end = to - direction * 6;
    return {...edge, id: `${edge.from}/${edge.to}`, color: order(edge.from), channel,
      points: [point(from, baseline), point(start, baseline), point(start, level), point(finish, level), point(finish, baseline), point(to, baseline)],
      arrow: [point(from === to ? to + 10 : end - direction * 4, baseline - 4), point(from === to ? to + 6 : end, baseline),
        point(from === to ? to + 10 : end - direction * 4, baseline + 4)]};
  });
  return {routes, nodes: ROLE_NAMES.map((role, color) => ({role, color, x: position(role), y: baseline,
    active: pairs.some(edge => edge.from === role || edge.to === role), source: pairs.some(edge => edge.from === role)}))};
}
