import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";

const reports:any[]=[];
const messages=Array.from({length:16},(_,i)=>[
 {role:"user",message_id:`u${i}`,turn_id:`t${i}`,content:`original request ${i}`,timestamp:"2026-11-01T00:00:00Z"},
 {role:"assistant",message_id:`outcome:t${i}`,turn_id:`t${i}`,kind:"outcome",status:"completed",timestamp:"2026-11-01T00:00:01Z",
  content:Array.from({length:18},(_,line)=>`answer ${i}, original line ${line}: retained readable content`).join("\n")},
]).flat();

for(const verbose of [false,true])test(`full App scrolling survives empty-to-hydrated history and reverses without a frozen viewport (verbose=${verbose})`,async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:120,rows:32});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 let release:((value:any)=>void)|undefined;const calls:string[]=[],gaps:number[]=[];
 const client:any={ready:true,close(){},subscribeEvents(){return()=>{};},async callTool(op:string){calls.push(op);
  if(op==="config.get")return {language:"en",verbose,auto_compact:true};
  if(op==="project.overview")return {projects:[{name:"history",workspace:"C:/synthetic-history",runtime_summary:{state_available:true,daemon_online:false}}]};
  if(op==="project.enter")return {ok:true,name:"history"};
  if(op==="runtime.status")return new Promise(resolve=>{release=resolve;});
  if(op==="lesson.harvest.list")return {proposals:[]};return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),wait=(ms=40)=>new Promise(r=>setTimeout(r,ms));
 const nodes=(node:any):any[]=>[node,...(node.childNodes||[]).flatMap(nodes)];
 const viewport=()=>nodes(ink.rootNode).find(n=>n.style?.overflowY==="scroll"&&n.scrollHeight>n.scrollViewportHeight);
 const frame=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||" ").join("")).join("\n");};
 let pulse:any;
 try{
  ink.setAltScreenActive(true);ink.onRender();await wait(80);stdin.write("\r");await wait(60);expect(release).toBeDefined();
  release!({state_available:true,daemon_online:false,storage:{level:"ok"},processes:{},main_conversation:messages});await wait(100);
  expect(viewport()).toBeDefined();expect(frame()).toContain('answer 15, original line 17');
  // Initial history now opens at the latest reply. Read back to its start
  // before exercising the original down/up reversal through real wheel input.
  const initial=viewport(),rewind=Math.ceil((initial.scrollHeight-initial.scrollViewportHeight)/3)+1;
  stdin.write('\x1b[<64;40;9M'.repeat(rewind));await wait(180);
  expect(viewport().renderedScrollTop).toBeLessThan(2);
  let previous=performance.now();pulse=setInterval(()=>{const now=performance.now();gaps.push(now-previous);previous=now;},10);
  for(let i=0;i<50;i++){stdin.write("\x1b[<65;40;9M");await wait(5);}
  await wait(120);const down=viewport();expect(down.scrollTop).toBeGreaterThan(down.scrollViewportHeight*2);
  expect(Math.abs(down.scrollTop-down.renderedScrollTop)).toBeLessThan(2);
  const downTop=down.scrollTop,downPaint=down.renderedScrollTop;
  const screen=ink.frontFrame.screen;let readable=0;
  for(let y=down.scrollViewportTop;y<down.scrollViewportTop+down.scrollViewportHeight;y++){
   const line=Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||" ").join("");if(line.includes("original line"))readable++;
  }
  expect(readable).toBeGreaterThan(5);
  for(let i=0;i<50;i++){stdin.write("\x1b[<64;40;9M");await wait(5);}
  await wait(120);const up=viewport();expect(up.scrollTop).toBeLessThan(2);expect(up.renderedScrollTop).toBeLessThan(2);
  stdin.write("draft is still responsive");await wait();expect(frame()).toContain("draft is still responsive");
  expect(calls).not.toContain("runtime.chat");expect(calls).not.toContain("runtime.trace");
  const sorted=[...gaps].sort((a,b)=>a-b);expect(Math.max(...gaps)).toBeLessThan(250);
  reports.push({verbose,down_top:downTop,down_paint:downPaint,readable_rows:readable,
   ui_gap_max_ms:Math.max(...gaps),ui_gap_p95_ms:sorted[Math.floor(sorted.length*.95)],
   evidence:"full App, delayed history, real wheel parser and actual frame cells; not formal terminal visual acceptance"});
  if(process.env.GITGO_SCROLL_PERF_REPORT)await Bun.write(process.env.GITGO_SCROLL_PERF_REPORT,JSON.stringify(reports,null,2));
 }finally{if(pulse)clearInterval(pulse);root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
