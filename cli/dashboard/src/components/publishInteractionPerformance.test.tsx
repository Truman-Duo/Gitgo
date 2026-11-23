import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {InputProvider} from "../input/runtime.js";
import {PublishWorkspacePanel} from "./PublishWorkspacePanel.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {createPublishStateStore} from "../state/publishLocalState.js";
import {configureTerminalGraphics} from "../ui/terminalGraphics.js";

test("Sixel workspace navigation retains the graph raster, coalesces disk saves and keeps input updates flowing",async()=>{
 const beforeColor=process.env.GITGO_NO_COLOR;delete process.env.GITGO_NO_COLOR;configureTerminalGraphics(true);
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:120,rows:38});
 stdout.on("data",(chunk:Buffer)=>{const text=chunk.toString();if(text.includes("\x1b[16t"))stdin.write("\x1b[6;16;8t");if(text.includes("\x1b[c")||text.includes("\x1b[0c"))stdin.write("\x1b[?64;4c");});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 let calls=0,saves=0;
 const local=createPublishStateStore().scope("performance");const save=local.set;local.set=(key,value)=>{saves++;save(key,value);};
 const client:any={async callTool(op:string){calls++;return op==="publish.items"?{schema_version:1,snapshot_id:"items",items:[],next_cursor:null}:
  {schema_version:1,snapshot_id:"tree",head_oid:"a",commits:[{oid:"a",parents:["b"],subject:"first"},{oid:"b",parents:["root"],subject:"second"}],formal_commits:[],formal_total:0,next_cursor:null};}};
 function Fixture(){const input=useTextInput();return <Box height={38} width={120} flexDirection="column"><PublishWorkspacePanel client={client} project="performance" localState={local} cmdInput={input} onFooter={()=>{}} onDismiss={()=>{}} onOpenTrial={()=>{}} onOpenFormal={()=>{}}/></Box>;}
 const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);const wait=(ms:number)=>new Promise(r=>setTimeout(r,ms));
 function raster(node:any):any{return node.attributes?.terminalRaster??node.childNodes?.map(raster).find(Boolean);}
 try{
  ink.setAltScreenActive(true);ink.onRender();
  for(let i=0;i<20&&!raster(ink.rootNode);i++)await wait(40);
  const image=raster(ink.rootNode);expect(image).toBeDefined();
  stdin.write("\r");await wait(190);saves=0;const beforeCalls=calls;
  const gaps:number[]=[];let previous=performance.now();const pulse=setInterval(()=>{const now=performance.now();gaps.push(now-previous);previous=now;},10);
  try{for(let i=0;i<24;i++){stdin.write(i%2?"\x1b[A":"\x1b[B");await wait(15);expect(raster(ink.rootNode)).toBe(image);}}finally{clearInterval(pulse);}
  await wait(180);expect(saves).toBeLessThan(4);expect(calls).toBe(beforeCalls);
  const ordered=[...gaps].sort((a,b)=>a-b),p95=ordered[Math.min(ordered.length-1,Math.floor(ordered.length*.95))]!;
  expect(gaps.length).toBeGreaterThan(10);expect(Math.max(...gaps)).toBeLessThan(250);expect(p95).toBeLessThan(100);
  if(process.env.GITGO_PUBLISH_PERF_REPORT)await Bun.write(process.env.GITGO_PUBLISH_PERF_REPORT,JSON.stringify({
   evidence:"shared Workbench and actual Sixel raster with simulated terminal device replies; not formal terminal or LLM latency",navigation_inputs:24,
   raster_reused:true,coalesced_saves:saves,backend_calls_during_navigation:calls-beforeCalls,ui_pulse_max_ms:Math.max(...gaps),ui_pulse_p95_ms:p95},null,2));
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();configureTerminalGraphics(false);
  if(beforeColor===undefined)delete process.env.GITGO_NO_COLOR;else process.env.GITGO_NO_COLOR=beforeColor;}
});
