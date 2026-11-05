import React from "react";
import {test,expect} from "bun:test";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";

const wait=()=>new Promise(resolve=>setTimeout(resolve,100));
for(const managed of [false,true])test(`complete App /update can navigate or browse the actual ${managed?"managed":"source-only"} status`,async()=>{
 const previous=chalk.level;chalk.level=3;
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:44,rows:10});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const status:any={schema_version:1,product_id:"gitgo-terminal",version:"1.0.0-alpha.3",status:"disabled",enabled:false,automatic_check:false,
  channel:"stable",can_apply:false,blockers:["MANAGED_INSTALLATION_REQUIRED"]};
 if(managed)Object.assign(status,{schema_version:6,status:"idle",can_check:true,can_download:false,can_request_apply:false,can_request_rollback:false,
  rollback:null,apply_strategy:"immediate",pending_restart:null,latest_version:null,candidate:null,automatic_install_available:true,
  preferences:{schema_version:1,revision:0,channel:"stable",automatic_check:false,automatic_download:false,automatic_install:false},
  automatic:{state:"not_checked",last_attempt:null,next_attempt:null,error_code:null}});
 const calls:string[]=[];
 const client:any={ready:true,close(){},async callTool(name:string){calls.push(name);
  if(name==="project.overview")return {projects:[{name:"example",workspace:"C:/example",runtime_summary:{state_available:true,daemon_online:false}}]};
  if(name==="config.get")return {language:"zh",verbose:false,auto_compact:true};
  if(name==="update.status")return status;
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(keys:string)=>{stdin.write(keys);await wait();};
 const text=()=>{const screen=ink.frontFrame.screen;return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join("")).join("\n");};
 const scroll=(node:any):any=>node.scrollViewportHeight!==undefined?node:node.childNodes?.map(scroll).find(Boolean);
 try{
  ink.setAltScreenActive(true);ink.onRender();await wait();await send("/");await send("update");await send("\r");
  expect(text()).toContain("软件更新");const node=scroll(ink.rootNode);expect(node).toBeDefined();
  if(managed){
    await send("\x1b[B".repeat(3));await send("\r");await send("\x1b[C");
    expect(text()).toContain("开启"); // Draft changed, with no write before confirmation.
    await send("\x1b");expect(text()).not.toContain("开启");
  }
  else {expect(node.scrollTop).toBe(0);await send("\x1b[B".repeat(100));expect(node.scrollTop).toBe(Math.max(0,node.scrollHeight-node.scrollViewportHeight));
    expect(node.scrollTop).toBeGreaterThan(0);await send("\x1b[A".repeat(100));expect(node.scrollTop).toBe(0);}
  expect(calls.filter(name=>name.startsWith("update."))).toEqual(["update.status"]);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;}
});
