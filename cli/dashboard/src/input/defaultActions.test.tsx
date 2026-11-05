import React from "react";
import {test, expect, spyOn} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync,Text} from "@anthropic/ink";
import {InputProvider,useManagedInput} from "./runtime.js";

test("application consumption precedes exact Unix Ctrl+Z suspension",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=80;stdout.rows=24;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 let consume=true,suspends=0;
 const events:any[]=[];
 const originalKill=process.kill;
 const kill=spyOn(process,"kill").mockImplementation((pid,signal)=>{
  if(pid===process.pid&&signal==="SIGSTOP"){suspends++;return true;}
  return originalKill.call(process,pid,signal);
 });
 function Fixture(){
  useManagedInput((input,key)=>{events.push({input,key});return consume;},{raw:true});
  return <Text>Input ownership</Text>;
 }
 const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const wait=()=>new Promise(resolve=>setTimeout(resolve,40));
 try{
  await wait();
  stdin.write("\x1a");await wait();
  stdin.write("\x1b[122;5u");await wait();
  expect(events.map(event=>event.input)).toEqual(["z","z"]);
  expect(suspends).toBe(0);
  consume=false;
  for(const sequence of ["\x1b[122;6u","\x1b[122;7u","\x1b[122;13u","\x1b[200~\x1a\x1b[201~"]){stdin.write(sequence);await wait();}
  expect(events).toHaveLength(6);
  expect(events[2].key.shift).toBe(true);
  expect(events.at(-1).key.paste).toBe(true);
  expect(suspends).toBe(0);
  stdin.write("\x1a");await wait();
  expect(suspends).toBe(process.platform==="win32"?0:1);
  if(suspends)process.emit("SIGCONT");
 }finally{
  root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();kill.mockRestore();
 }
});
