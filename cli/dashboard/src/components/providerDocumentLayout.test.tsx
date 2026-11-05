import React from "react";
import chalk from "chalk";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";

// Full production component tree and renderer-buffer checks, not terminal visual acceptance.
test("formal App keeps delayed discovery errors separate from API field editing and stages a changed URL",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:30});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});const calls:any[]=[];let rejectOld:any;
 const client:BackendClient={ready:true,close(){},async callTool(op,args:any={}){
  calls.push([op,args]);if(op==="project.overview")return {projects:[]};if(op==="config.get")return {language:"en",verbose:false};
  if(op==="provider.status")return {active_provider:"existing",providers:[{id:"existing",name:"Existing",model_id:"old"}]};
  if(op==="provider.presets")return {presets:[{id:"openai",name:"OpenAI",base_url:"https://api.test/v1",protocol:"openai_responses"}]};
  if(op==="provider.models"){
   if(!args.draft.api_key&&!args.draft.base_url.endsWith("/proxy"))return new Promise((_,reject)=>rejectOld=reject);
   if(!args.draft.base_url.endsWith("/proxy"))throw Error("MODEL_LIST_HTTP_401: Discovery/test unavailable. Draft retained");
   return {models:[{id:"synthetic-gpt",limits:{}}]};
  }
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,100));};
 const screen=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||" ").join("")).join("\n");};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send("");await send("/config providers");await send("\r");await send("\r");await send("/new");await send("\r");await send("OpenAI");await send("\r");
  await send("\r");rejectOld(Error("MODEL_LIST_HTTP_401: late old result"));await send("");
  expect(screen()).not.toContain("Operation failed");expect(screen()).not.toContain("Enter stage field");
  await send("synthetic-secret");await send("\r");expect(screen()).toContain("Model list authentication failed");
  await send("\x1b[A");await send("\r");expect(screen()).not.toContain("Model list authentication failed");
  await send("/proxy");await send("\x13");expect(screen()).toContain("Add models");expect(screen()).toContain("synthetic-gpt");expect(screen()).not.toContain("Operation failed");
  expect(calls.filter(c=>c[0]==="provider.models").at(-1)[1].draft.base_url).toBe("https://api.test/v1/proxy");
  await send("\r");await send("\x13");await send("\x13");
  expect(calls.filter(c=>c[0]==="provider.save")).toHaveLength(1);
  expect(calls.find(c=>c[0]==="provider.save")[1]).toMatchObject({base_url:"https://api.test/v1/proxy",name:"OpenAI",model_id:"synthetic-gpt"});
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);

test("formal App JSON keeps Dracula syntax, no visible position counter and bounded scrolling through resize",async()=>{
 const previous=chalk.level;chalk.level=3;
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:92,rows:30});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});const calls:any[]=[];
 const models=["one","two"].map(model_id=>({model_id,protocol:"openai_chat",context_window:128000,max_output_tokens:4096,request_preferences:{}}));
 const provider={id:"a",name:"Layout API",model_id:"one",base_url:"https://api.test/v1",protocol:"openai_chat",context_window:128000,max_output_tokens:4096,revision:"r",configuration:{models}};
 const client:BackendClient={ready:true,close(){},async callTool(op,args:any={}){
  calls.push([op,args]);if(op==="project.overview")return {projects:[]};
  if(op==="config.get")return {language:"en",verbose:false};
  if(op==="provider.status")return {active_provider:"a",providers:[provider]};
  if(op==="provider.models")return {models:[]};
  if(op==="provider.connection")return {checks:{model_response:"verified"}};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),wait=()=>new Promise(r=>setTimeout(r,100)),send=async(text:string)=>{stdin.write(text);await wait();};
 const lines=()=>{const screen=ink.frontFrame.screen;return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char || " ").join(""));};
 const foreground=(needle:string)=>{const rows=lines(),y=rows.findIndex(line=>line.includes(needle));expect(y).toBeGreaterThanOrEqual(0);const x=rows[y]!.indexOf(needle),codes=ink.stylePool.get(cellAt(ink.frontFrame.screen,x,y)!.styleId).map((s:any)=>s.code).join("");return codes;};
 const checkFooter=()=>{const rows=lines();expect(rows.some(line=>/^\s*\d+–\d+\/\d+/u.test(line))).toBe(false);expect(rows.at(-1)!.trim()).toBe("");expect(ink.frontFrame.screen.height).toBe(stdout.rows);};
 const borderCount=()=>lines().filter(line=>/^\s*─{40,}\s*$/u.test(line)).length;
 try{
  ink.setAltScreenActive(true);ink.onRender();await wait();await send("/config providers");await send("\r");
  expect(borderCount()).toBe(1); // Header focus: provider commands do not own input yet.
  await send("\r");expect(borderCount()).toBe(3); // Empty, active CommandBar remains visible.
  await send("/");expect(lines().some(line=>line.includes("/new"))).toBe(true);
  await send("\x1b");expect(borderCount()).toBe(3);expect(lines().some(line=>line.includes("/new"))).toBe(false);
  await send("/edit");await send("\r");
  for(let i=0;i<5;i++)await send("\t");await send("\r");expect(lines().some(line=>line.includes("Edit all model parameters here"))).toBe(false);
  for(let i=0;i<3;i++)await send("\t");await send("\r");await wait();checkFooter();
  expect(foreground('"name"')).toContain("38;2;80;250;123");
  expect(foreground('"Layout API"')).toContain("38;2;241;250;140");
  expect(foreground("128000")).toContain("38;2;189;147;249");
  expect(foreground('"Layout API"')).not.toMatch(/48;2;/);
  const document=()=>lines().slice(1,-1).join("\n");const initialRange=document();
  await send("\x1b[<65;5;8M");
  expect(document()).not.toBe(initialRange);
  expect(ink.cursorDeclaration.visible).toBe(false);
  await send("\x1b[<64;5;8M");expect(document()).toBe(initialRange);
  stdout.columns=52;stdout.rows=18;stdout.emit("resize");await wait();await wait();checkFooter();
  await send("\x1b[<65;5;8M".repeat(40));const bottomRange=document();
  await send("\x1b[<65;5;8M");expect(document()).toBe(bottomRange);
  await send("\x1b[<64;5;8M");expect(document()).not.toBe(bottomRange);
  await send("\x1b[C");expect(ink.cursorDeclaration.visible).toBe(true);checkFooter();
  await send("\x1b[D");
  await send("\x1b[F");checkFooter();expect(lines().some(line=>line.includes("}"))).toBe(true);
  await send("\x13");const saved=calls.find(c=>c[0]==="provider.save")[1];expect(saved.models).toHaveLength(2);expect(JSON.stringify(saved)).not.toContain("\\u001b");
  expect(lines().some(line=>line.includes("Edit API connection"))).toBe(true);
  expect(lines().some(line=>line.includes("Edit all model parameters here"))).toBe(false);
  await send("\x1b");expect(lines().some(line=>line.includes("Automatic failover"))).toBe(true);expect(borderCount()).toBe(3);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;}
},15000);
