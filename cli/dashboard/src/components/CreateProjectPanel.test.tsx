import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {InputProvider} from "../input/runtime.js";
import {CreateProjectPanel} from "./CreateProjectPanel.js";

function mount(client: BackendClient, onCreated: (name: string) => void, settings?: any,clipboardReader?:()=>Promise<string>) {
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:160,rows:30});
  const stderr:any=new PassThrough();let output="";stdout.on("data",(chunk: Buffer)=>output+=chunk);stderr.on("data",()=>{});
  let dismisses=0;
  function Fixture(){const input=useTextInput();return <InputProvider settings={settings} clipboardReader={clipboardReader}><CreateProjectPanel client={client} defaultWorkspace="C:/existing" cmdInput={input} onFooter={()=>{}} onDismiss={()=>dismisses++} onCreated={onCreated}/></InputProvider>;}
  const root=renderSync(<Fixture/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  return {output:()=>output,dismisses:()=>dismisses,async send(keys:string){stdin.write(keys);await new Promise(r=>setTimeout(r,65));},close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}

test("a clipboard command requested in Name cannot arrive later in the workspace field",async()=>{
 const calls:any[]=[];let resolveClipboard:any;
 const ui=mount({ready:true,close(){},async callTool(_,args){calls.push(args);return {ok:true};}},()=>{},undefined,()=>new Promise(resolve=>resolveClipboard=resolve));
 try{
  await ui.send('\r');await ui.send('deliberate-name');await ui.send('\x16');
  await ui.send('\r');await ui.send('\t');await ui.send('\t');await ui.send('\r');
  resolveClipboard('/create');await ui.send('');await ui.send('\x13');
  expect(calls).toHaveLength(1);expect(calls[0]).toMatchObject({name:'deliberate-name',workspace_path:'C:/existing'});
  expect(ui.output()).not.toContain('/create');
 }finally{ui.close();}
});
test("field editing is explicit, Enter stages, save retries visibly without losing values",async()=>{
  const calls:any[]=[];const created:string[]=[];
  const ui=mount({ready:true,close(){},async callTool(operation,args){expect(operation).toBe("project.create");calls.push(args);if(calls.length===1)throw new Error("WORKSPACE_ALREADY_REGISTERED: Existing is archived; restore it from Bin");return {ok:true};}},name=>created.push(name));
  try{
    await ui.send("ignored");await ui.send("\r");await ui.send("demo");await ui.send("\r");
    for(let n=0;n<3;n++)await ui.send("\t");
    await ui.send("\r");await ui.send("https://example.test/release");await ui.send("\r");
    expect(calls).toHaveLength(0);expect(ui.output()).not.toContain("Ctrl+S");expect(ui.output()).not.toContain("▸");
    await ui.send("\x13");expect(calls).toHaveLength(1);expect(created).toEqual([]);
    expect(ui.output()).toContain("WORKSPACE_ALREADY_REGISTERED");expect(ui.output()).toContain("restore it from Bin");
    await ui.send("\x1b[A");await ui.send("\r");await ui.send("-new");await ui.send("\r");
    await ui.send("\x13");expect(calls).toHaveLength(2);expect(created).toEqual(["demo"]);
    expect(calls[1]).toMatchObject({name:"demo",workspace_path:"C:/existing-new",workspace_mode:"attach_existing",release_url:"https://example.test/release"});
    expect(calls[1]).not.toHaveProperty("llm_provider");
  }finally{ui.close();}
});
test("Esc rolls back only the current text or choice; rebinding save applies to live form",async()=>{
  const calls:any[]=[];
  const ui=mount({ready:true,close(){},async callTool(_,args){calls.push(args);return {ok:true};}},()=>{}, {version:1,overrides:{"form.save":{key:"s",meta:true}}});
  try{
    await ui.send("\r");await ui.send("demo");await ui.send("\r");
    await ui.send("\r");await ui.send("-discard");await ui.send("\x1b");expect(ui.dismisses()).toBe(0);
    await ui.send("\t");await ui.send("\r");await ui.send("\x1b[C");await ui.send("\x1b");
    await ui.send("\x13");expect(calls).toHaveLength(0);
    await ui.send("\x1bs");expect(calls).toHaveLength(1);expect(calls[0]).toMatchObject({name:"demo",workspace_mode:"attach_existing"});
    expect(ui.output()).not.toContain("Alt+S");
  }finally{ui.close();}
});
test("validation is visible and consecutive saves cannot duplicate an in-flight request",async()=>{
  let resolve:any;const calls:any[]=[];
  const ui=mount({ready:true,close(){},async callTool(_,args){calls.push(args);return new Promise(r=>resolve=r);}},()=>{});
  try{
    await ui.send("\x13");expect(calls).toHaveLength(0);expect(ui.output()).toContain("Name and workspace are required");
    await ui.send("\r");await ui.send("demo");
    await ui.send("\x13");await ui.send("\x13");expect(calls).toHaveLength(1);expect(calls[0].name).toBe("demo");
    resolve({ok:true});await ui.send("");
  }finally{ui.close();}
});
