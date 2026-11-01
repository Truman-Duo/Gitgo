import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync,Text} from "@anthropic/ink";
import {KeybindingsTab} from "./KeybindingsTab.js";
import {InputProvider,useBindingSettings,useManagedInput} from "../../input/runtime.js";
import {applicationBindings} from "../../input/router.js";
import {useGeneralConfig} from "../../hooks/useGeneralConfig.js";
import {useTextInput} from "../../hooks/useTextInput.js";
import {chordLabel} from "../../input/bindings.js";
import type {BackendClient} from "../../backend/client.js";

// Real Ink decoder and production router; no claim of terminal visual acceptance.
test("capture validates raw keys, preserves paste, saves atomically, reloads and resets",async()=>{
 let stored:any={version:1,overrides:{}};
 const writes:any[]=[],events:any[]=[];
 let reject=false;
 const client:BackendClient={ready:true,close(){},async callTool(operation,args={}){
  if(operation==="config.get")return {keyboard_bindings:stored};
  if(operation==="config.set"){if(reject)throw Error("disk failure");stored=args.value;writes.push(args);return {ok:true};}
  return {};
 }};
 const wait=()=>new Promise(r=>setTimeout(r,90));
 async function mount(){
  const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
  const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=100;stdout.rows=30;
  let output="";stdout.on("data",(s:Buffer)=>output+=s.toString());
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  function Fixture(){
   const config=useGeneralConfig(client),input=useTextInput();useBindingSettings(config.keyboardBindings);
   useManagedInput((input,key)=>{if(key.escape){events.push({input,key});return true;}return false;},{priority:1000});
   return <><KeybindingsTab client={client} project="" cmdInput={input} onFooter={()=>{}} report={()=>{}}
    contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/><Text>{chordLabel("escape")}</Text></>;
  }
  const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  await wait();return {output:()=>output,async key(value:string){stdin.write(value);await wait();},close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
 }
 let terminal=await mount();
 try{
  await terminal.key("\r"); // standalone detection
  await new Promise(r=>setTimeout(r,5150));expect(terminal.output()).toContain("No key detected yet");expect(writes).toHaveLength(0);
  await terminal.key("\x1b[106;9u");expect(terminal.output()).toContain("Super+J");
  await terminal.key("\x1b[106;6u");expect(terminal.output()).toContain("Ctrl+Shift+J");expect(writes).toHaveLength(0);
  await terminal.key("\x1b");
  await terminal.key("\x1b[B");await terminal.key("\x1b[B");await terminal.key("\r"); // interrupt action
  await terminal.key("\x1b[200~pasted\x1b[201~");expect(writes).toHaveLength(0);
  await terminal.key("\x03");expect(terminal.output()).toContain("Key already used");expect(writes).toHaveLength(0);
  await terminal.key("\x18");expect(terminal.output()).toContain("Received Ctrl+X");expect(writes).toHaveLength(0);
  reject=true;await terminal.key("\r");expect(terminal.output()).toContain("disk failure");expect(applicationBindings.label("execution.interrupt")).toBe("Esc");
  reject=false;await terminal.key("\r");await wait();expect(stored.overrides["execution.interrupt"]).toEqual({key:"x",ctrl:true});
  expect(applicationBindings.label("execution.interrupt")).toBe("Ctrl+X");
  await terminal.key("\x18");expect(events.at(-1).key.escape).toBe(true);
 }finally{terminal.close();}
 terminal=await mount();
 try{
  expect(applicationBindings.label("execution.interrupt")).toBe("Ctrl+X");
  await terminal.key("\x1b[B");await terminal.key("\r");await wait();expect(stored).toEqual({version:1,overrides:{}});
  expect(applicationBindings.label("execution.interrupt")).toBe("Esc");expect(writes).toHaveLength(2);
 }finally{terminal.close();}
},15000);

import {Box} from "@anthropic/ink";
import instances from "../../../vendor/ink/src/core/instances.js";

test("held navigation and paging stop at edges, reverse immediately, and keep selection visible after resize",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=true;stdout.columns=90;stdout.rows=16;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const writes:any[]=[];
 const client:BackendClient={ready:true,close(){},async callTool(operation,args={}){
  if(operation==="config.get")return {keyboard_bindings:{version:1,overrides:{}}};
  if(operation==="config.set"){writes.push(args.value);return {ok:true};}return {};
 }};
 const noop=()=>{};
 const shell={back:noop,goToTab:noop,tabPrev:noop,tabNext:noop,leaveContent:noop};
 function Fixture({width,height}:{width:number;height:number}){
  const input=useTextInput();
  return <Box width={width} height={height} flexDirection="column"><InputProvider><KeybindingsTab client={client} project="" cmdInput={input}
   onFooter={noop} report={noop} contentFocused shell={shell}/></InputProvider></Box>;
 }
 const root=renderSync(<Fixture width={90} height={16}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const wait=()=>new Promise(r=>setTimeout(r,80));
 const ink=instances.get(stdout) as any;
 const findScroll=(node:any):any=>node.scrollViewportHeight!==undefined?node:node.childNodes?.map(findScroll).find(Boolean);
 const bottom=(node:any)=>Math.max(0,node.scrollHeight-node.scrollViewportHeight);
 const original=ink.rootNode.onRender;
 let schedules=0;
 ink.rootNode.onRender=(...args:any[])=>{schedules++;return original?.(...args);};
 try{
  await wait();
  const scroll=findScroll(ink.rootNode);expect(scroll.scrollViewportHeight).toBeGreaterThan(0);
  stdin.write("\x1b[B".repeat(4));await wait();expect(scroll.scrollTop).toBe(0); // visible rows do not scroll
  stdin.write("\x1b[B".repeat(100));await wait();expect(scroll.scrollTop).toBe(bottom(scroll));
  const before=schedules;
  stdin.write("\x1b[B".repeat(500));await wait();
  expect(scroll.scrollTop).toBe(bottom(scroll));expect(scroll.pendingScrollDelta||0).toBe(0);
  expect(schedules-before).toBeLessThanOrEqual(1); // no scroll requests for a clamped selection
  const pageBefore=schedules;
  stdin.write("\x1b[6~".repeat(500));await wait();
  expect(scroll.pendingScrollDelta||0).toBe(0);expect(schedules-pageBefore).toBe(0);
  stdin.write("\x1b[5~".repeat(20));await wait();expect(scroll.scrollTop).toBe(0);
  stdin.write("\x1b[B");await wait();expect(scroll.scrollTop).toBe(bottom(scroll)); // paging must not strand selection
  stdin.write("\x1b[A");await wait();stdin.write("\r");await wait();
  stdin.write("\x1b[122;6u");await wait();stdin.write("\r");await wait();
  expect(writes.at(-1).overrides["lesson.toggle"]).toEqual({key:"z",ctrl:true,shift:true});
  stdin.write("\x1b[A".repeat(100));await wait();expect(scroll.scrollTop).toBe(0);
  const topBefore=schedules;
  stdin.write("\x1b[A".repeat(500));stdin.write("\x1b[5~".repeat(500));await wait();
  expect(scroll.scrollTop).toBe(0);expect(scroll.pendingScrollDelta||0).toBe(0);expect(schedules-topBefore).toBeLessThanOrEqual(1);
  stdin.write("\x1b[6~".repeat(20));await wait();expect(scroll.scrollTop).toBe(bottom(scroll));
  stdin.write("\x1b[A");await wait();expect(scroll.scrollTop).toBe(0);
  stdin.write("\x1b[B".repeat(100));await wait();
  stdout.columns=50;stdout.rows=10;stdout.emit("resize");root.rerender(<Fixture width={50} height={10}/>);await wait();
  expect(scroll.scrollTop).toBe(bottom(scroll));expect(scroll.pendingScrollDelta||0).toBe(0);
 }finally{ink.rootNode.onRender=original;root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);
