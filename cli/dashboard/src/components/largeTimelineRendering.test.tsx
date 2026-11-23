import React, {createRef,useState} from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, Text, ScrollBox, renderSync,getElementRect, type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {ContentViewport} from "../ui/ContentViewport.js";
import {TurnTimeline} from "./TurnTimeline.js";
import {MarkdownText} from "./MarkdownText.js";
import {RequestContextRow,inputPageDisplayText} from "./RequestContextRow.js";
import {traceBodyReader} from "../chat/traceSource.js";
import type {ToolCallCard} from "../types.js";
import {InputProvider} from "../input/runtime.js";
const wait = () => new Promise(resolve=>setTimeout(resolve,100));
const count = (node: any): number => 1 + (node.childNodes || []).reduce((n: number, child: any)=>n+count(child),0);
function mount(content: React.ReactNode) {
  const stdin: any = new PassThrough(); Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  let output="";
  const stdout: any = new PassThrough(); Object.assign(stdout,{isTTY:false,columns:100,rows:20}); stdout.on("data",(data:Buffer)=>{output=(output+data.toString()).slice(-6000);});
  let errors="";
  const stderr: any = new PassThrough(); stderr.on("data",(data:Buffer)=>{errors+=data.toString();});
  const scroll = createRef<ScrollBoxHandle>();
  const draw=(value:React.ReactNode)=><InputProvider><Box width={100} height={20} flexDirection="column"><ContentViewport scrollRef={scroll}>
    <ScrollBox ref={scroll} flexGrow={1} flexDirection="column">{value}</ScrollBox>
  </ContentViewport></Box></InputProvider>;
  const root = renderSync(draw(content),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const ink = () => {const instance=instances.get(stdout) as any;if(!instance)throw new Error("Renderer stopped: "+errors+output);return instance;};
  ink().setAltScreenActive(true);ink().onRender();
  return {stdin,scroll,tree:()=>ink().rootNode,nodeCount:()=>count(ink().rootNode), frame:()=>{
    const screen=ink().frontFrame.screen;
    return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char??" ").join("")).join("\n");
  },close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("all ten thousand diff lines remain reachable while only a viewport of cells mounts",async()=>{
  const diff="--- a/file.ts\n+++ b/file.ts\n@@ -1,5000 +1,5000 @@\n"+Array.from({length:5000},(_,index)=>
    "-const before"+index+"=1;\n+const "+(index===4999?"FINAL_DIFF_LINE":"after"+index)+"=2;").join("\n");
  const tool:ToolCallCard={tool_name:"edit_file",target:"file.ts",allowed:true,status_label:"OK",duration_ms:1,timestamp:"",
    state:"completed",diff};
  const io=mount(<TurnTimeline activity={[{kind:"tool",toolIndex:0}]} tools={[tool]} notices={[]} verbose width={96}/>);
  try {
    await wait(); expect(io.frame()).not.toContain("FINAL_DIFF_LINE"); expect(io.nodeCount()).toBeLessThan(2000);
    const lines=io.frame().split("\n"),row=lines.findIndex(line=>line.includes("edit_file")),col=lines[row]!.indexOf("▸");
    io.stdin.write("\x1b[<0;"+(col+1)+";"+(row+1)+"M\x1b[<0;"+(col+1)+";"+(row+1)+"m");
    await new Promise(resolve=>setTimeout(resolve,250));
    const nodes=(node:any):any[]=>[node,...(node.childNodes||[]).flatMap(nodes)];
    const inner=nodes(io.tree()).find(node=>node.style?.overflowY==="scroll"&&node.scrollViewportHeight<=8&&node.scrollHeight>100);
    expect(inner).toBeDefined();const rect=getElementRect(inner)!;
    io.stdin.write("\x1b[<35;"+(Math.floor(rect.x)+2)+";"+(Math.floor(rect.y)+2)+"M");
    await wait();io.stdin.write("\x1b[F");await wait();
    expect(io.frame()).toContain("FINAL_DIFF_LINE"); expect(io.nodeCount()).toBeLessThan(2000);
    io.stdin.write("\x1b[H");await wait();
    expect(io.frame()).toContain("before0");expect(io.frame()).not.toContain("FINAL_DIFF_LINE");
  }finally{io.close();}
});

test("paged injected source keeps Unicode graphemes intact at transport boundaries",()=>{
  const text="x".repeat(8189)+"👨‍👩‍👧‍👦 e\u0301\r\nEND",points=[...text],rendered=[];
  for(let offset=0;offset<points.length;offset+=8192){
    const end=Math.min(points.length,offset+8192);
    rendered.push(inputPageDisplayText({source_index:0,offset,next_offset:end,total_chars:points.length,has_more:end<points.length,
      text:points.slice(offset,end).join(""),before:points.slice(Math.max(0,offset-64),offset).join(""),after:points.slice(end,end+64).join("")}));
  }
  expect(rendered.join("")).toBe(text);
});

test("long injected input stays virtual and token updates continue while its first and final pages are read",async()=>{
  const content=Array.from({length:30000},(_,index)=>`<rules>原始输入 ${index} ${"x".repeat(35)}</rules>`).join("\n")+"\nFINAL_INJECTED_SOURCE";
  const offsets:number[]=[],packets:number[]=[],gaps:number[]=[];let tokens=0,push!:(value:number)=>void;
  const client:any={callTool:async(name:string,args:any)=>{
    expect(name).toBe("runtime.trace");expect(args.action).toBe("detail");
    let detail:any;
    if(args.view==="input_sources")detail={representation:"canonical_provider_input_sources",input_provenance:{schema_version:1,sources:[
      {message_index:0,sections:[{name:"AGENTS.md",version:"saved"}]}]}};
    else{
      expect(args.view).toBe("input_source");const offset=args.offset,end=Math.min(content.length,offset+8192);offsets.push(offset);
      detail={representation:"canonical_provider_input_source_page",source_index:0,offset,next_offset:end,total_chars:content.length,has_more:end<content.length,
        text:content.slice(offset,end),before:content.slice(Math.max(0,offset-64),offset),after:content.slice(end,end+64)};
    }
    packets.push(Buffer.byteLength(JSON.stringify(detail)));return {detail};
  }};
  const read=traceBodyReader(client,"large-source","turn");
  function Probe(){const [value,setValue]=useState(0);push=setValue;return <Box flexDirection="column">
    <Text>{`STREAM TOKEN ${value}`}</Text><RequestContextRow request={{event:"provider_request_started",detail_ref:"source"}} readBody={read} width={96}/></Box>;}
  const io=mount(<Probe/>);
  const allText=(node:any):string=>node.nodeValue ?? (node.childNodes ?? []).map(allText).join("");
  const nodes=(node:any):any[]=>[node,...(node.childNodes ?? []).flatMap(nodes)];
  const click=async(label:string)=>{for(let attempt=0;attempt<10;attempt++){
    const node=nodes(io.tree()).find(node=>node.attributes?.clickMode==='action'&&node._eventHandlers?.onClick&&allText(node).includes(label));
    const rect=node&&getElementRect(node);if(rect){
     io.stdin.write(`\x1b[<0;${Math.floor(rect.x)+1};${Math.floor(rect.y)+1}M\x1b[<0;${Math.floor(rect.x)+1};${Math.floor(rect.y)+1}m`);return;
    }await wait();
   }throw new Error('Visible action unavailable: '+label);};
  let pulse:ReturnType<typeof setInterval>|undefined;
  try{
    await wait();expect(packets).toHaveLength(0);await click("Injected context");await wait();expect(offsets).toHaveLength(0);
    let last=performance.now();pulse=setInterval(()=>{const now=performance.now();gaps.push(now-last);last=now;push(++tokens);},10);
    await click("AGENTS.md");await new Promise(resolve=>setTimeout(resolve,300));
    expect(tokens).toBeGreaterThan(5);expect(Math.max(...gaps)).toBeLessThan(250);
    clearInterval(pulse);pulse=undefined;
    expect(io.nodeCount()).toBeLessThan(1200);expect(io.frame()).not.toContain("FINAL_INJECTED_SOURCE");
    const window=nodes(io.tree()).find(node=>node.style?.borderStyle==='single'&&nodes(node).some(child=>child.style?.overflowY==='scroll'));
    expect(window).toBeDefined();
    const viewport=nodes(window).find(node=>node.style?.overflowY==='scroll');
    expect(viewport.yogaNode.getComputedHeight()).toBeLessThanOrEqual(16);
    expect(viewport.yogaNode.getComputedHeight()).toBeGreaterThan(8);
    const rect=getElementRect(window)!;
    io.stdin.write(`\x1b[<35;${Math.floor(rect.x)+2};${Math.floor(rect.y)+2}M`);await wait();
    for(let attempt=0;attempt<6 && !io.frame().includes("FINAL_INJECTED_SOURCE");attempt++){io.stdin.write('\x1b[F');await wait();}
    expect(io.frame()).toContain("FINAL_INJECTED_SOURCE");expect(io.nodeCount()).toBeLessThan(1200);
    expect(offsets.length).toBeLessThan(20);expect(Math.max(...packets)).toBeLessThan(50000);
    const openNodes=io.nodeCount(),windowRows=viewport.yogaNode.getComputedHeight();
    expect(io.scroll.current!.getScrollTop()).toBeLessThanOrEqual(5);
    io.stdin.write('\x1b[H');await wait();expect(io.frame()).toContain('原始输入 0');
    io.stdin.write('\x1b');await wait();expect(io.frame()).toContain('AGENTS.md');expect(io.frame()).not.toContain('原始输入 0');
    const readAtClose=offsets.length;await wait();expect(offsets).toHaveLength(readAtClose);
    if(process.env.GITGO_CONTEXT_PERF_REPORT)await Bun.write(process.env.GITGO_CONTEXT_PERF_REPORT,JSON.stringify({
      source_characters:content.length,source_bytes:Buffer.byteLength(content),body_pages_read:offsets.length,body_pages_total:Math.ceil(content.length/8192),
      largest_packet_bytes:Math.max(...packets),dom_nodes:openNodes,closed_dom_nodes:io.nodeCount(),window_rows:windowRows,simulated_token_updates:tokens,
      ui_pulse_max_ms:Math.max(...gaps),ui_pulse_p95_ms:gaps.slice().sort((a,b)=>a-b)[Math.floor(gaps.length*.95)],
      final_source_reachable:true,evidence:"component viewport and simulated token cadence; not formal-terminal or real-API latency"},null,2));
  }finally{if(pulse)clearInterval(pulse);io.close();}
},10000);
test("large plain reasoning preserves Unicode and its last source line without a reading cutoff",async()=>{
  const content=Array.from({length:10000},(_,index)=>"中文 e\u0301 👨‍👩‍👧‍👦 行 "+index).join("\n")+"\n源文件末行";
  const io=mount(<MarkdownText content={content} width={96} dimColor/>);
  try {
    await wait(); expect(io.frame()).toContain("中文"); expect(io.nodeCount()).toBeLessThan(500);
    io.scroll.current!.scrollToBottom(); await wait();
    expect(io.frame()).toContain("源文件末行"); expect(io.nodeCount()).toBeLessThan(500);
  }finally{io.close();}
});
