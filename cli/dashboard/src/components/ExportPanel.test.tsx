import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {ExportPanel} from "./ExportPanel.js";

test("export uses explicit field edits and one form save; custom paths survive option edits",async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:120,rows:30});
  const stderr:any=new PassThrough();let output="";stdout.on("data",(chunk:Buffer)=>output+=chunk);stderr.on("data",()=>{});
  const calls:any[]=[];let dismisses=0;let release:any;
  const client:any={ready:true,close(){},async callTool(operation:string,args:any){expect(operation).toBe("project.export");calls.push(args);if(calls.length===1)throw new Error("EXPORT_WRITE_FAILED");return new Promise(resolve=>release=resolve);}};
  function Fixture(){const input=useTextInput();return <InputProvider><ExportPanel client={client} project="demo" cols={120} cmdInput={input} onFooter={()=>{}} onDismiss={()=>dismisses++}/></InputProvider>;}
  const root=renderSync(<Fixture/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const send=async(keys:string)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,65));};
  try{
    await send("\x1b[C");await send("\r");await send("\x1b[C");await send("\x1b");
    await send("\t");await send("\t");await send("\r");await send("\x01");
    // Replace the default using the existing editor's Home/Delete operations.
    await send("\x1b[H");for(let i=0;i<33;i++)await send("\x1b[3~");
    await send("custom/export.json");await send("\r");
    await send("\x1b[A");await send("\r");await send("\x1b[C");await send("\r");
    expect(calls).toHaveLength(0);await send("\x13");expect(output).toContain("EXPORT_WRITE_FAILED");
    expect(calls[0]).toMatchObject({minimal:true,output_path:"custom/export.json",output_format:"yaml"});
    await send("\x13");await send("\x13");expect(calls).toHaveLength(2);
    release({path:"custom/export.json",size:12,sha256:"123"});await send("");
    await send("x");expect(dismisses).toBe(0);await send("\r");expect(dismisses).toBe(1);
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
