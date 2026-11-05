import {test, expect} from "bun:test";
import {layoutCommitGraph, drawCommitGraph, COMMIT_SPACING} from "./publishCommitGraph.js";
import {railCrossings} from "./publishGraphRoutes.js";

const diamond = [{oid:"merge",parents:["left","right"]},{oid:"left",parents:["root"]},
  {oid:"right",parents:["root"]},{oid:"root",parents:[]}];
const complex = [{oid:"m",parents:["a","b","c"]},{oid:"a",parents:["r"]},
  {oid:"b",parents:["later"]},{oid:"c",parents:["r"]},{oid:"r",parents:[]},{oid:"later",parents:[]},
  {oid:"tip",parents:["unknown"]}];

test("shared parents keep parallel tracks until the actual parent node", () => {
  const beforeRoot = layoutCommitGraph(diamond.slice(0,3));
  expect(beforeRoot.frontier.lanes).toEqual([{awaiting:"root",colorIdx:0},{awaiting:"root",colorIdx:1}]);
  expect(beforeRoot.rails.filter(rail=>rail.id.includes("join"))).toHaveLength(0);
  const graph = layoutCommitGraph(diamond);
  expect(graph.nodes.map(row => row.lane)).toEqual([0,0,1,0]);
  expect(graph.rails.filter(rail=>rail.id.includes("join")).map(rail=>rail.id)).toEqual(["3/join/1"]);
  expect(graph.frontier.lanes).toEqual([]);
  const text=drawCommitGraph(graph,2,80,15).join("\n");
  expect(text).toContain("O");expect(text).toContain("02");
});

test("all page cuts preserve node lanes, colors and solid routes without mutating saved state", () => {
  for(const commits of [diamond,complex]) {
    const whole=layoutCommitGraph(commits);
    for(let cut=1;cut<commits.length;cut++){
      const first=layoutCommitGraph(commits.slice(0,cut)), saved=structuredClone(first.frontier);
      const second=layoutCommitGraph(commits.slice(cut),first.frontier,"m");
      expect([...first.nodes,...second.nodes].map(row=>[row.oid,row.lane,row.color,row.parents])).toEqual(whole.nodes.map(row=>[row.oid,row.lane,row.color,row.parents]));
      const split=[...first.rails.filter(r=>!r.pending),...second.rails.filter(r=>!r.pending).map(r=>({...r,
        points:r.points.map(p=>({...p,x:p.x+cut*COMMIT_SPACING}))}))];
      expect(split.map(({color,points})=>({color,points}))).toEqual(whole.rails.filter(r=>!r.pending).map(({color,points})=>({color,points})));
      expect(first.frontier).toEqual(saved);expect(second.frontier).toEqual(whole.frontier);
    }
  }
  expect(drawCommitGraph(layoutCommitGraph(diamond.slice(0,1)),0,40,6).join("\n")).toContain(">");
});

test("orthogonal routes use local bends and distinguish crossings from real joins",()=>{
  const graph=layoutCommitGraph(complex), crossings=railCrossings(graph.rails);
  expect(crossings.length).toBeGreaterThan(0);
  expect(crossings.some(cross=>cross.over.id==="4/join/2"&&cross.under.id==="4/pass/1")).toBe(true);
  for(const rail of graph.rails) for(let i=1;i<rail.points.length;i++) {
    const a=rail.points[i-1]!,b=rail.points[i]!;
    expect(a.x===b.x||a.y===b.y).toBe(true);
    expect(Math.abs(a.x-b.x)).toBeLessThanOrEqual(COMMIT_SPACING);
  }
  expect(graph.nodes.slice(0,2).map(row=>row.lane)).toEqual([0,0]);
  expect(graph.nodes.slice(0,2).map(row=>row.color)).toEqual([0,0]);
});

test("criss-cross and octopus forks never reuse an occupied parent track",()=>{
  const graph=layoutCommitGraph([{oid:"m1",parents:["a","b","c"]},{oid:"m2",parents:["d","b","b"]},
    {oid:"a",parents:[]},{oid:"b",parents:[]},{oid:"c",parents:[]},{oid:"d",parents:[]},{oid:"orphan",parents:[]}]);
  expect(graph.nodes[0]!.parents.map(parent=>parent.lane)).toEqual([0,1,2]);
  expect(graph.nodes[1]!.parents.map(parent=>parent.oid)).toEqual(["d","b"]);
  expect(graph.nodes[1]!.parents[1]!.lane).not.toBe(1);
  expect(graph.frontier.lanes).toEqual([]);
  expect(graph.nodes.at(-1)!.parents).toEqual([]);
  const rich=layoutCommitGraph([{oid:"old",parents:[]}],{lanes:[null,{awaiting:"old",colorIdx:7}],nextColor:8});
  expect(rich.nodes[0]).toMatchObject({lane:1,color:7});
  expect(layoutCommitGraph([{oid:"old",parents:[]}],[null,"old"]).nodes[0]!.lane).toBe(1);
  const narrow=drawCommitGraph(graph,4,20,3);
  expect(narrow).toHaveLength(3);expect(narrow.join("\n")).toContain("O");
  expect(narrow.every(line=>line.length<=20)).toBe(true);
  expect(drawCommitGraph(layoutCommitGraph([]),0,80,10)).toEqual([]);
});
