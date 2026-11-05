/** Stroke/circle styling ported from oil-oil/oil-git src/HistoryGraph.tsx.
 * Copyright (c) 2026 Zhihuang Lin (oil-oil), MIT; vendor/oil-git/LICENSE.
 * Both diagrams share orthogonal rail geometry, SVG, resvg antialiasing and Sixel transport.
 */
import {COMMIT_SPACING, LANE_MARGIN, LANE_SPACING, type CommitGraph} from "./publishCommitGraph.js";
import {roundedRail, railCrossings, layoutRoleRoutes, type Rail} from "./publishGraphRoutes.js";
import type {GraphicsCapability} from "../ui/terminalGraphics.js";
import type {SvgScene} from "../ui/svgRaster.js";
import {colors} from "../theme/index.js";

function viewport(columns: number, rows: number, capability: GraphicsCapability) {
  const width = Math.max(1, Math.min(2048, Math.floor(columns * capability.cellWidth)));
  const height = Math.max(1, Math.min(340, Math.floor(rows * capability.cellHeight), Math.floor(524288 / width)));
  return {width, height};
}
function document(width: number, height: number, content: string): SvgScene {
  return {width, height, svg: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${content}</svg>`};
}
const color = (index: number) => colors.graph.lanes[index % colors.graph.lanes.length]!;
const stroke = (rail: Rail) => `stroke="${color(rail.color)}" stroke-width="1.6" opacity="0.8" fill="none"${rail.pending ? ' stroke-dasharray="4 4"' : ""}`;
function railsSvg(rails: Rail[]) {
  return rails.map(rail => `<path d="${roundedRail(rail.points)}" ${stroke(rail)}/>`).join("") +
    railCrossings(rails).map(cross => `<path d="M${cross.x} ${cross.y - 4}V${cross.y + 4}" stroke="${colors.graph.background}" stroke-width="5.6"/>
      <path d="M${cross.x} ${cross.y - 4}V${cross.y + 4}" ${stroke(cross.over)}/>`).join("");
}
function node(x: number, y: number, color: string, selected: boolean, head: boolean) {
  return `<g transform="translate(${x} ${y})">${head ? `<circle r="11" fill="${color}" opacity="0.13"/>` : ""}
    <circle r="5" stroke="${color}" stroke-width="1.8" fill="${selected ? color : colors.graph.background}"/></g>`;
}

/** Horizontal time axis, persistent parallel lanes, local forks/joins at real nodes only. */
export function commitGraphSvg(graph: CommitGraph, selected: number, columns: number, rows: number, capability: GraphicsCapability): SvgScene {
  const {width, height} = viewport(columns, rows, capability), scale = Math.max(.75, capability.cellHeight / 24);
  const visible = Math.max(1, Math.floor(width / (COMMIT_SPACING * scale)));
  const lanes = Math.max(1, Math.floor((height / scale - 2 * LANE_MARGIN) / LANE_SPACING) + 1);
  const start = Math.max(0, Math.min(selected - Math.floor(visible / 2), graph.nodes.length - visible));
  const end = Math.min(graph.nodes.length, start + visible), selectedLane = graph.nodes[selected]?.lane ?? 0;
  const top = Math.max(0, Math.min(selectedLane - Math.floor(lanes / 2), graph.lanes - lanes));
  const rails = graph.rails.filter(rail => rail.points.some(p => p.x >= start * COMMIT_SPACING && p.x <= (end + 1) * COMMIT_SPACING));
  const nodes = graph.nodes.slice(start, end + 1).map(commit => node((commit.column + .5) * COMMIT_SPACING,
    LANE_MARGIN + commit.lane * LANE_SPACING, color(commit.color), commit.column === selected, commit.oid === graph.head)).join("");
  return document(width, height, `<g transform="matrix(${scale} 0 0 ${scale} ${-start * COMMIT_SPACING * scale} ${-top * LANE_SPACING * scale})">${railsSvg(rails)}${nodes}</g>`);
}

/** Business-plan edges remain distinct from Git ancestry; geometry and styling are shared. */
export function roleGraphSvg(edges: {from: string; to: string}[], columns: number, rows: number, capability: GraphicsCapability): SvgScene {
  const {width, height} = viewport(columns, rows, capability), layout = layoutRoleRoutes(edges, width, height);
  const arrows = new Map(layout.routes.map(route => [JSON.stringify(route.arrow), route]));
  return document(width, height, railsSvg(layout.routes) + [...arrows.values()].map(route =>
    `<path d="M${route.arrow.map(p => `${p.x} ${p.y}`).join("L")}" ${stroke({...route, color: layout.nodes.find(n => n.role === route.to)!.color})}/>`).join("") +
    layout.nodes.map(n => node(n.x, n.y, n.active ? color(n.color) : colors.graph.inactive, n.source, n.source)).join(""));
}

/** Solid rails are actual parents; dashed cross-band rails are selected-source provenance. */
export function workspaceGraphSvg(process: CommitGraph, formal: CommitGraph, links: {oid: string; sources: string[]}[],
  processSelected: number, formalSelected: number, band: "process" | "formal", columns: number, rows: number, capability: GraphicsCapability): SvgScene {
  const {width, height} = viewport(columns, rows, capability);
  const top = height * .2, bottom = height * .8, spacing = Math.max(48, COMMIT_SPACING * .75);
  const count = Math.max(1, Math.floor(width / spacing));
  const start = (graph: CommitGraph, selected: number) => Math.max(0, Math.min(selected - Math.floor(count / 2), graph.nodes.length - count));
  const a = start(process, processSelected), b = start(formal, formalSelected);
  const x = (index: number, offset: number) => (index - offset + .5) * spacing;
  const visibleLanes = Math.max(1, Math.floor(height * .25 / 14));
  const topLane = (graph: CommitGraph, selected: number) => Math.max(0, Math.min((graph.nodes[selected]?.lane ?? 0) - Math.floor(visibleLanes / 2), graph.lanes - visibleLanes));
  const ys = (graph: CommitGraph, base: number, index: number, selected: number) => base + ((graph.nodes[index]?.lane ?? 0) - topLane(graph, selected)) * 14;
  const draw = (graph: CommitGraph, offset: number, base: number, selected: number, active: boolean) => {
    const transform = `matrix(${spacing / COMMIT_SPACING} 0 0 ${14 / LANE_SPACING} ${-offset * spacing} ${base - LANE_MARGIN * 14 / LANE_SPACING - topLane(graph, selected) * 14})`;
    const clipY = base === top ? 0 : height * .65, clipHeight = base === top ? height * .4 : height * .35;
    return `<svg y="${clipY}" width="${width}" height="${clipHeight}" viewBox="0 ${clipY} ${width} ${clipHeight}"><g transform="${transform}">${railsSvg(graph.rails)}</g>` + graph.nodes.slice(offset, offset + count).map(n =>
      node(x(n.column, offset), ys(graph, base, n.column, selected), color(n.color), active && n.column === selected, n.oid === graph.head)).join("") + "</svg>";
  };
  const selectedOid = band === "formal" ? formal.nodes[formalSelected]?.oid : process.nodes[processSelected]?.oid;
  const provenance = links.flatMap(link => {
    if (band === "formal" ? selectedOid !== link.oid : !link.sources.includes(selectedOid ?? "")) return [];
    const fi = formal.nodes.findIndex(n => n.oid === link.oid);
    if (fi < b || fi >= b + count) return [];
    return link.sources.flatMap(source => {
      const pi = process.nodes.findIndex(n => n.oid === source);
      if (pi < a || pi >= a + count) return [];
      const selected = selectedOid === link.oid || selectedOid === source, middle = height * .5;
      return [`<path d="${roundedRail([{x: x(pi, a), y: ys(process, top, pi, processSelected) + 7}, {x: x(pi, a), y: middle}, {x: x(fi, b), y: middle}, {x: x(fi, b), y: ys(formal, bottom, fi, formalSelected) - 7}])}" stroke="${colors.graph.selected}" stroke-width="${selected ? 1.8 : 1}" stroke-dasharray="3 5" opacity="${selected ? .85 : .5}" fill="none"/>`];
    });
  }).join("");
  return document(width, height, provenance + draw(process, a, top, processSelected, band === "process") + draw(formal, b, bottom, formalSelected, band === "formal"));
}
