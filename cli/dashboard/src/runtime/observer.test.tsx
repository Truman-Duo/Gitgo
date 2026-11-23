import React from "react";
import {test} from "node:test";
import assert from "node:assert/strict";
import {PassThrough} from "node:stream";
import {renderSync,Text} from "@anthropic/ink";
import {InputProvider,useManagedInput} from "../input/runtime.js";
import {createFrontendObserver} from "./observer.js";
import type {BackendClient} from "../backend/client.js";

test("numeric observations are bounded, async and retain coverage of failed batches",async()=>{
  let time=0,resolveSend:(value:any)=>void=()=>{};
  const calls:any[]=[];
  const client={callTool:async(operation:string,args:any)=>{
    calls.push({operation,args});return new Promise(resolve=>{resolveSend=resolve;});
  }} as BackendClient;
  assert.equal(createFrontendObserver(client),undefined);
  const observer=createFrontendObserver(client,{enabled:true,now:()=>time,intervalMs:60000})!;
  for(let i=0;i<100;i++)observer.onInput({atMs:0,dispatchMs:1,kind:0,length:3,handled:1});
  time=25;observer.onFrame({durationMs:17,flickers:[]});
  const first=observer.flush();void observer.flush();
  assert.equal(calls.length,1);
  const batch=calls[0].args;
  assert.equal(batch.inputs.count,100);assert.equal(batch.input_to_frame.count,64);
  assert.equal(batch.dropped.pending_inputs,36);assert.equal(batch.frames.duration.max,17);
  assert.equal(batch.input_to_frame.max,25);
  resolveSend({captured:false});await first;
  const second=observer.flush();assert.equal(calls[1].args.dropped.batches,1);
  resolveSend({captured:true});await second;
  const closing=observer.close();resolveSend({captured:true});await closing;
  const flatten=(value:any):boolean=>typeof value==="number"||(Array.isArray(value)?value.every(flatten):
    value&&typeof value==="object"&&Object.values(value).every(flatten));
  assert.ok(calls.every(call=>call.operation==="observer.frontend"&&flatten(call.args)));
});

test("production Ink onFrame and input owner feed metrics without raw field text or duplicate routing",async()=>{
  const calls:any[]=[];
  const client={callTool:async(operation:string,args:any)=>{calls.push({operation,args});return{captured:true};}} as BackendClient;
  const observer=createFrontendObserver(client,{enabled:true,intervalMs:60000})!;
  const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
  const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=80;stdout.rows=24;
  const stderr:any=new PassThrough();stdout.on("data",()=>{});stderr.on("data",()=>{});
  const received:string[]=[];
  function Probe(){
    useManagedInput((text)=>{received.push(text);});
    return <Text>Normal production renderer and input provider</Text>;
  }
  const root=renderSync(<InputProvider observeInput={observer.onInput}><Probe/></InputProvider>,
    {stdin,stdout,stderr,onFrame:observer.onFrame,exitOnCtrlC:false,patchConsole:false});
  try{
    await new Promise(r=>setTimeout(r,40));stdin.write("private-field-content");
    await new Promise(r=>setTimeout(r,40));
    root.rerender(<InputProvider observeInput={observer.onInput}><Probe/></InputProvider>);
    await new Promise(r=>setTimeout(r,40));
    await observer.flush();
    assert.equal(received.join(""),"private-field-content");
    assert.ok(calls[0].args.frames.duration.count>0);assert.ok(calls[0].args.inputs.count>0);
    assert.ok(!JSON.stringify(calls).includes("private-field-content"));
  }finally{root.unmount();root.cleanup();await observer.close();stdin.destroy();stdout.destroy();stderr.destroy();}
});
