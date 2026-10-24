import React from "react";
import {test,expect} from "bun:test";
import {renderSync} from "@anthropic/ink";
import {PassThrough} from "node:stream";
import {RunningBStrip} from "./RunningBStrip.js";
import {runningWorkers} from "../daemon/agentLabels.js";
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
