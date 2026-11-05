import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {StatusItemList} from "./StatusItemList.js";

test("read-only status collection is bounded and keeps the currently edited item visible",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>{};stdin.ref=()=>{};stdin.unref=()=>{};
 const stdout:any=new PassThrough();stdout.columns=60;stdout.rows=12;let output="";stdout.on("data",(b:Buffer)=>output+=b);
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const items=Array.from({length:8},(_,i)=>({id:String(i),label:"model-"+i,state:"unknown" as const}));
 const root=renderSync(<StatusItemList title="Configured" items={items} currentId="7" maxVisible={2}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 try{await new Promise(r=>setTimeout(r,70));expect(output).toContain("Configured (8)");expect(output).toContain("model-0");expect(output).toContain("model-7");expect(output).not.toContain("model-1");expect(output).toContain("+6");}
 finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
