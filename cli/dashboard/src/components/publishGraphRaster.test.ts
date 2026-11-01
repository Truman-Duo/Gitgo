import {expect,test} from "bun:test";
import {commitGraphSvg,roleGraphSvg,workspaceGraphSvg} from "./publishGraphSvg.js";
import {layoutCommitGraph} from "./publishCommitGraph.js";
import {initializeSvgRenderer,renderSvgRaster} from "../ui/svgRaster.js";
import {encodeSixel,validRaster} from "../../vendor/ink/src/core/raster.js";
import {graphicsCapability} from "../../vendor/ink/src/hooks/use-terminal-graphics.js";
import {colors} from "../theme/index.js";

test("graphics require real Sixel and bounded character-cell replies",()=>{
  expect(graphicsCapability([64,1,4],8,16).sixel).toBe(true);
  for(const args of [[[4],8,16],[[64,1],8,16],[[64,4],0,0],[[64,4],800,800]] as const)
    expect(graphicsCapability([...args[0]],args[1],args[2]).sixel).toBe(false);
});

test("workspace bands show actual parents separately from selected-source provenance at terminal sizes", async () => {
  await initializeSvgRenderer();
  const process = layoutCommitGraph([{oid: "b", parents: ["a"]}, {oid: "a", parents: ["root"]}]);
  const formal = layoutCommitGraph([{oid: "semantic", parents: ["root"]}]);
  for (const [columns, rows] of [[120, 9], [80, 7], [40, 4]]) {
    const scene = workspaceGraphSvg(process, formal, [{oid: "semantic", sources: ["a", "b"]}], 1, 0, "formal", columns!, rows!, graphicsCapability([64, 4], 8, 16));
    expect(scene.svg).toContain('stroke-dasharray="3 5"');
    expect(scene.svg).not.toMatch(/url\(|<text|<image/);
    expect(validRaster(renderSvgRaster(scene))).toBe(true);
  }
});

test("normalized SVG rails retain Oil Git thin strokes, hollow nodes, selection and HEAD glow",async()=>{
  const graph=layoutCommitGraph([{oid:"merge",parents:["left","right"]},{oid:"left",parents:["root"]},
    {oid:"right",parents:["root"]},{oid:"root",parents:[]}]);
  const cap=graphicsCapability([64,4],8,16),scene=commitGraphSvg(graph,2,80,9,cap);
  expect(scene.svg).toContain('stroke-width="1.6" opacity="0.8"');
  expect(scene.svg).toContain('r="5"');
  expect(scene.svg).toContain('r="11"');
  expect(scene.svg).toContain('opacity="0.13"');
  expect(scene.svg).toContain('fill="'+colors.graph.lanes[1]+'"');
  expect(scene.svg).toContain('Q'); // local rounded corner, no long cubic bows
  expect(scene.svg).not.toMatch(/d="[^"]*C/);
  await initializeSvgRenderer();
  const raster=renderSvgRaster(scene);
  expect(validRaster(raster)).toBe(true);
  expect(new Set(raster.pixels).size).toBeGreaterThan(12); // antialias shades, without dithering
  expect(encodeSixel(raster)).toStartWith("\x1bP0;1;0q");
  expect(encodeSixel(raster)).toEndWith("\x1b\\");
  const narrow=renderSvgRaster(commitGraphSvg(graph,3,20,3,cap));
  expect(narrow.width).toBe(160);expect(narrow.height).toBe(48);
});

test("pending parents are dashed and nonparent repository text never enters SVG",()=>{
  const graph=layoutCommitGraph([{oid:'a<script>',parents:["pending"]}]);
  const scene=commitGraphSvg(graph,0,80,4,graphicsCapability([64,4],12,24));
  expect(scene.svg).toContain('stroke-dasharray="4 4"');
  expect(scene.svg).not.toContain("<script>");
});

test("SVG antialias compositing preserves premultiplied edge brightness without pixel dithering",async()=>{
  await initializeSvgRenderer();
  const image=renderSvgRaster({width:1,height:1,svg:`<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><path d="M0 0H1V1H0Z" fill="${colors.graph.lanes[0]}" opacity="0.5"/></svg>`});
  const hex=image.palette[image.pixels[0]!]!;
  const green=parseInt(hex.slice(3,5),16);
  expect(green).toBeGreaterThan(98);expect(green).toBeLessThan(110);
});

test("reverse and same-role business links use the SVG style without creating Git parents",async()=>{
  await initializeSvgRenderer();
  for(const edges of [[{from:"trial",to:"workspace"}],[{from:"workspace",to:"workspace"}],[]]){
    const scene=roleGraphSvg(edges,80,5,graphicsCapability([64,4],12,24));
    expect(validRaster(renderSvgRaster(scene))).toBe(true);
  }
  expect(()=>renderSvgRaster({svg:'<svg><image href="file:///private"/></svg>',width:16,height:16})).toThrow("INVALID_GRAPH_SVG");
  expect(()=>renderSvgRaster({svg:"<svg/>",width:99999,height:16})).toThrow("INVALID_GRAPH_SVG");
});
