import React from "react";
import {test,expect} from "bun:test";
import {renderSync} from "@anthropic/ink";
import {PassThrough} from "node:stream";
import {RunningBStrip} from "./RunningBStrip.js";
import {runningWorkers} from "../daemon/agentLabels.js";
import chalk from "chalk";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
test("footer includes actual running and waiting workers, excludes terminal and main",()=>{
 const process=(id:string,status:string,parent_id:string|null="a")=>({process_id:id,status,parent_id,actor_kind:parent_id?"worker":"supervisor",display_name:id,created_at:id}) as any;
 const workers=runningWorkers({a:process("main","running",null),b:process("active","running"),c:process("queued","waiting"),d:process("done","completed")});
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.columns=140;stdout.rows=10;stdout.isTTY=false;
 const stderr:any=new PassThrough();let text="";stdout.on("data",(chunk:Buffer)=>{text+=chunk.toString();});stderr.on("data",()=>{});
 const root=renderSync(<RunningBStrip runningB={workers} selIdx={0} focused={false} width={140} language="zh"/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();
 expect(text).toContain("active");expect(text).toContain("工作中");expect(text).toContain("queued");expect(text).toContain("等待");expect(text).not.toContain("done");
});


test("narrow footer keeps pending status and removes navigation instructions",()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.columns=36;stdout.rows=4;stdout.isTTY=false;
 const stderr:any=new PassThrough();let text="";stdout.on("data",(b:Buffer)=>{text+=b.toString();});stderr.on("data",()=>{});
 const worker:any={process_id:"b",display_name:"a very long subprocess name",parent_id:"a",status:"awaiting_user",actor_kind:"worker"};
 const root=renderSync(<RunningBStrip runningB={[worker]} selIdx={0} focused width={36} language="zh" contextPct="2.7%" statusText="工作中"/>,
  {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 try{
  expect(text).toContain("待答复");expect(text).toContain("2.7%");
  expect(text).not.toContain("/processlist");expect(text).not.toContain("输入对话");
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("mode occupies the second slot without a background and keeps worker positions stable",async()=>{
 const previous=chalk.level;chalk.level=3;
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{columns:90,rows:4,isTTY:false});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const workers:any[]=[{process_id:"b1",display_name:"B1",parent_id:"a",status:"running",actor_kind:"worker"},
   {process_id:"b2",display_name:"B2",parent_id:"a",status:"awaiting_user",actor_kind:"worker"}];
 const draw=(mode:"auto"|"manual",width=90)=><RunningBStrip runningB={workers} selIdx={1} focused width={width}
   advancement={mode} contextPct="0.1%" statusText="Completed"/>;
 const root=renderSync(draw("auto"),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);
 const inspect=()=>{const screen=ink.frontFrame.screen;return {screen,line:Array.from({length:screen.width},(_,x)=>cellAt(screen,x,0)?.char??" ").join("")};};
 try{
  await new Promise(r=>setTimeout(r,60));const first=inspect();
  expect(first.line).toMatch(/^● 0\.1% · AUTO\s+· B1/);expect(first.line).not.toContain("Completed");
  const workerX=first.line.indexOf("B1");
  for(const [mode,fg] of [["auto","253;188;43"],["manual","201;209;217"]] as const){
   root.rerender(draw(mode));await new Promise(r=>setTimeout(r,60));const {screen,line}=inspect();
   expect(line.indexOf("B1")).toBe(workerX);expect(line).toContain("B2");
   const index=line.indexOf(mode.toUpperCase());expect(index).toBeGreaterThan(0);
   const codes=ink.stylePool.get(cellAt(screen,index,0)!.styleId).map((c:any)=>c.code).join("");
   expect(codes).toContain(`\x1b[38;2;${fg}m`);expect(codes).not.toContain("\x1b[48;");
  }
  root.rerender(draw("manual",36));await new Promise(r=>setTimeout(r,60));
  const narrow=inspect().line;expect(narrow).toContain("MANUAL");expect(narrow).toContain("B2");
  expect(narrow.slice(36).trim()).toBe("");
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;}
});

test("provisional and failed closeout remain visible in the main conversation strip",()=>{
 const process=(id:string,status:string)=>({process_id:id,status,parent_id:"a",actor_kind:"worker",created_at:id}) as any;
 const rows:any={pending:{...process("p","completed"),closeout_state:"pending"},
  failed:{...process("f","failed"),closeout_state:"failed"},
  done:{...process("d","completed"),closeout_state:"finalized"}};
 expect(runningWorkers(rows).map(p=>p.process_id).sort()).toEqual(["f","p"]);
});
