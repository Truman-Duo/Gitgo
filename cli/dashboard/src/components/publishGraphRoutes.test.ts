import {test,expect} from "bun:test";
import {layoutRoleRoutes,roundedRail,railCrossings,point,ROLE_NAMES} from "./publishGraphRoutes.js";

test("local rounding and overpasses do not invent a junction at unrelated crossings",()=>{
  expect(roundedRail([point(0,0),point(20,0),point(20,30),point(40,30)]))
    .toBe("M0 0L15 0Q20 0 20 5L20 25Q20 30 25 30L40 30");
  const horizontal={id:"h",color:0,points:[point(0,20),point(40,20)]};
  const vertical={id:"v",color:1,points:[point(20,0),point(20,40)]};
  expect(railCrossings([horizontal,vertical])).toMatchObject([{x:20,y:20,over:{id:"v"},under:{id:"h"}}]);
  expect(railCrossings([horizontal,{...vertical,points:[point(20,20),point(20,40)]}])).toEqual([]);
});

test("role plans use stable positions and unique ordered channels regardless of input order",()=>{
  const edges=[{from:"workspace",to:"formal"},{from:"trial",to:"formal"},{from:"workspace",to:"formal"},
    {from:"trial",to:"workspace"},{from:"workspace",to:"workspace"}];
  const a=layoutRoleRoutes(edges,960,120), b=layoutRoleRoutes([...edges].reverse(),960,120);
  expect(a).toEqual(b);expect(a.routes).toHaveLength(4);
  expect(a.routes.find(route=>route.id==="workspace/formal")!.count).toBe(2);
  expect(a.nodes.map(node=>[node.role,node.x,node.y])).toEqual([["workspace",160,72],["trial",480,72],["formal",800,72]]);
  expect(a.nodes.map(node=>[node.x,node.y])).toEqual(layoutRoleRoutes([],960,120).nodes.map(node=>[node.x,node.y]));
  const channels=a.routes.map(route=>route.points[2]!.y);
  expect(new Set(channels).size).toBe(channels.length);
  expect(a.routes.find(route=>route.id==="trial/workspace")!.points[2]!.y).toBeGreaterThan(72);
});

test("all role directions and loops are bounded, orthogonal and clear the middle role",()=>{
  const edges=ROLE_NAMES.flatMap(from=>ROLE_NAMES.map(to=>({from,to})));
  for(const [width,height] of [[160,48],[640,72],[1440,168]]) {
    const layout=layoutRoleRoutes(edges,width!,height!);
    expect(layout.routes).toHaveLength(9);
    for(const route of layout.routes) {
      for(let i=0;i<route.points.length;i++) {
        const p=route.points[i]!,q=route.points[i+1];
        expect(p.x).toBeGreaterThanOrEqual(0);expect(p.x).toBeLessThanOrEqual(width!);
        expect(p.y).toBeGreaterThanOrEqual(0);expect(p.y).toBeLessThanOrEqual(height!);
        if(q) expect(p.x===q.x||p.y===q.y).toBe(true);
      }
      const middle=layout.nodes[1]!;
      if(route.from==="workspace"&&route.to==="formal") expect(route.points[2]!.y).not.toBe(middle.y);
    }
  }
  expect(layoutRoleRoutes([{from:"invented",to:"formal"}],640,72).routes).toEqual([]);
});

test("two contributions into Formal use nested channels without unnecessary crossings",()=>{
  const layout=layoutRoleRoutes([{from:"workspace",to:"formal"},{from:"trial",to:"formal"}],960,120);
  expect(railCrossings(layout.routes)).toEqual([]);
  expect(layout.routes.find(r=>r.from==="workspace")!.points[2]!.y)
    .toBeLessThan(layout.routes.find(r=>r.from==="trial")!.points[2]!.y);
});
