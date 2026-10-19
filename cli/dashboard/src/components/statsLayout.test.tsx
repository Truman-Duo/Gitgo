import React from "react";
import {test,expect} from "bun:test";
import {Box,renderSync} from "@anthropic/ink";
import {PassThrough} from "node:stream";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {StatsPanel} from "./StatsPanel.js";
import type {BackendClient} from "../backend/client.js";

// Inspect rendered cells, rather than concatenated frames, to catch real wrapping.
test("waterfall keeps call, tokens and timing on one aligned line at narrow and wide widths",async()=>{
 for(const width of [60,120]){
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:width,rows:28});stdout.on('data',()=>{});
  const stderr:any=new PassThrough();stderr.on('data',()=>{});
  const client:BackendClient={ready:true,close(){},async callTool(op){return op==='runtime.usage'?{tasks:[{task_id:'test',status:'completed',provider_calls:1,tool_calls:1}],page:{}}:{events:[
   {seq:1,event:'provider_request_started',process_id:'only-process',step:1,time:'2026-10-17T00:00:00Z'},
   {seq:2,event:'provider_usage',process_id:'only-process',usage:{input_tokens:100,output_tokens:20}},
   {seq:3,event:'provider_response_completed',process_id:'only-process',time:'2026-10-17T00:00:01Z'},
   {seq:4,event:'tool_result',process_id:'only-process',tool_name:'read_file',duration_ms:100,time:'2026-10-17T00:00:01.1Z'},
  ]};}};
  const root=renderSync(<InputProvider><Box height={28} width={width}><StatsPanel client={client} project="sample" cols={width} onDismiss={()=>{}}/></Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  try{
   await new Promise(r=>setTimeout(r,120));
   const screen=(instances.get(stdout) as any).frontFrame.screen;
   const lines=Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||' ').join('').trimEnd());
   const model=lines.find(line=>line.includes('R1')&&line.includes('Model'))!;
   const tool=lines.find(line=>line.includes('read_file'))!;
   expect(model).toContain('100 / 20');expect(model).toContain('1.00s');
   expect(tool).toContain('0.10s');expect(tool.indexOf('read_file')).toBe(model.indexOf('Model'));
   expect(lines.join('\n')).not.toContain('only-p');
   expect(lines.join('\n')).not.toContain('input 100');
   expect(lines.filter(line=>line.includes('100 / 20'))).toHaveLength(1);
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
 }
});
