import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {ModelPicker} from "./ModelPicker.js";

test("model selection uses only the current API snapshot and forwards its revision",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.columns=100;stdout.rows=25;let output="";stdout.on("data",(b:Buffer)=>output+=b);
 const stderr:any=new PassThrough();stderr.on("data",()=>{});const calls:any[]=[];let dismissed=0;
 const client:any={ready:true,close(){},async callTool(op:string,args:any){calls.push([op,args]);if(op==="provider.model.options")return {provider_id:"api-a",provider_name:"API A",revision:"version-a",model_id:"one",models:[{model_id:"one"},{model_id:"two"}]};if(op==="provider.model.select")return {status:"selected"};throw Error(op);}};
 const root=renderSync(<InputProvider><ModelPicker client={client} onFooter={()=>{}} onDismiss={()=>dismissed++}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const send=async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,75));};
 try{
  await send("");expect(output).toContain("API A");expect(output).toContain("one");expect(output).not.toContain("other-api");expect(output).not.toContain("1/2");
  await send("\x1b[B");await send("\r");
  expect(calls.map(c=>c[0])).toEqual(["provider.model.options","provider.model.select"]);
  expect(calls[1][1]).toEqual({provider_id:"api-a",model_id:"two",expected_revision:"version-a"});expect(dismissed).toBe(1);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
