import React,{useState} from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync,Text} from "@anthropic/ink";
import {InputProvider,InputSuspension,useManagedInput} from "./runtime.js";
import {useTextInput} from "../hooks/useTextInput.js";

for(const change of ["same","next-field","leave-and-return","disabled","unmounted","help-open","help-return"]){
 test(`delayed clipboard stays with its input target: ${change}`,async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{columns:80,rows:20});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  let resolveClipboard:any,currentValue="",control:any;
  function Editor({target,enabled}:{target:string;enabled:boolean}){
   const input=useTextInput("");currentValue=input.value;
   useManagedInput((text,key)=>{if(text&&!key.ctrl)input.insertText(text,{paste:key.paste});return true;},{inputTarget:target,clipboard:enabled});
   return <Text>{target}: {input.value}</Text>;
  }
  function Fixture(){const [target,setTarget]=useState("name"),[enabled,setEnabled]=useState(true),[shown,setShown]=useState(true),[tick,setTick]=useState(0),[suspended,setSuspended]=useState(false);control={setTarget,setEnabled,setShown,setTick,setSuspended};return shown?<InputSuspension suspended={suspended}><Editor target={target} enabled={enabled}/></InputSuspension>:<Text>closed {tick}</Text>;}
  const root=renderSync(<InputProvider clipboardReader={()=>new Promise(resolve=>resolveClipboard=resolve)}><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const wait=()=>new Promise(r=>setTimeout(r,45));
  try{
   await wait();stdin.write("\x16");await wait();expect(typeof resolveClipboard).toBe("function");
   if(change==="same"){control.setTick(1);await wait();}
   if(change==="next-field"||change==="leave-and-return"){control.setTarget("workspace");await wait();}
   if(change==="leave-and-return"){control.setTarget("name");await wait();}
   if(change==="disabled"){control.setEnabled(false);await wait();}
   if(change==="unmounted"){control.setShown(false);await wait();}
   if(change==="help-open"||change==="help-return"){control.setSuspended(true);await wait();}
   if(change==="help-return"){control.setSuspended(false);await wait();}
   resolveClipboard("/create literal paste");await wait();
   expect(currentValue).toBe(change==="same"?"/create literal paste":"");
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
 });
}
