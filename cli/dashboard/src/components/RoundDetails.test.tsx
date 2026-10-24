import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {RoundDetails} from "./RoundDetails.js";
import type {ChatMessage} from "../types.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 60));
function text(node:any):string{return node.nodeValue || (node.childNodes||[]).map(text).join("");}
function terminal(){
 const stdin:any = new PassThrough(); Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any = new PassThrough(); Object.assign(stdout,{isTTY:false,columns:90,rows:24});
 const stderr:any = new PassThrough(); stdout.on("data",()=>{});stderr.on("data",()=>{});
 return {stdin,stdout,stderr};
}
const message:ChatMessage={role:"assistant",content:"done",timestamp:"now",status:"completed",trace_id:"trace",process_id:"b"};

test("closing retains the loaded trajectory and reopening/remounting reuses its bounded cache", async()=>{
 const io=terminal(); let calls=0;
 const client:any={async callTool(){calls++;return {events:[{seq:1,time:"now",event:"progress_summary",process_id:"b",message:"PINNED-CANDIDATE"}],has_more:false};}};
 const draw=(open:boolean)=><Box width={90}><RoundDetails message={message} client={client} project="sample" verbose={false} width={86} readTrace={open}/></Box>;
 let root=renderSync(draw(true),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();expect(text((instances.get(io.stdout) as any).rootNode)).toContain("PINNED-CANDIDATE");
  root.rerender(draw(false));await wait();
  expect(text((instances.get(io.stdout) as any).rootNode)).toContain("PINNED-CANDIDATE");
  root.rerender(draw(true));await wait();expect(calls).toBe(1);
  root.unmount();root.cleanup();
  root=renderSync(draw(true),{...io,patchConsole:false,exitOnCtrlC:false});await wait();
  expect(text((instances.get(io.stdout) as any).rootNode)).toContain("PINNED-CANDIDATE");
  expect(calls).toBe(1);
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});

test("a late result for another process never overwrites the current trajectory",async()=>{
 const io=terminal();let finish:any;
 const client:any={async callTool(_op:any,args:any){
  if(args.process_id==="old")return new Promise(resolve=>{finish=resolve;});
  return {events:[{seq:1,time:"now",event:"progress_summary",process_id:"b",message:"CURRENT-PROCESS"}],has_more:false};
 }};
 const draw=(process_id:string)=><Box width={90}><RoundDetails message={{...message,process_id}} client={client} project="sample" verbose={false} width={86}/></Box>;
 const root=renderSync(draw("old"),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();root.rerender(draw("b"));await wait();
  finish({events:[{seq:1,time:"now",event:"progress_summary",process_id:"old",message:"STALE-PROCESS"}],has_more:false});
  await wait();const value=text((instances.get(io.stdout) as any).rootNode);
  expect(value).toContain("CURRENT-PROCESS");expect(value).not.toContain("STALE-PROCESS");
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});
