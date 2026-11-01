import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,Text,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {RuntimeMenu} from "./RuntimeMenu.js";
const wait=()=>new Promise(r=>setTimeout(r,65));
test("runtime header and divider occupy separate rows across child changes and resizing",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=true;stdout.columns=120;stdout.rows=24;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const process:any={process_id:"a",parent_id:null,actor_kind:"supervisor",created_at:"1",display_name:"Main",
  context:{limit:10000,used_tokens:500,breakdown:{sections:Array.from({length:35},(_,i)=>({name:"Section "+i,estimated_tokens:10}))}}};
 const client:any={ready:true,close(){},async callTool(operation:string){return operation==="runtime.tools.list"?{tools:[]}:
  operation==="memory.snapshots"?{snapshots:[]}:{};}};
 function Fixture({width,height}:{width:number;height:number}){
  const input=useTextInput();
  return <LanguageContext.Provider value="zh"><InputProvider><Box width={width} height={height} flexDirection="column">
   <Box flexGrow={1} minHeight={0}><RuntimeMenu cols={width} rows={height} client={client} project="demo" processes={{a:process}}
    cmdInput={input} onFooter={()=>{}} onSelect={()=>{}} onDismiss={()=>{}}/></Box><Text>STATUS</Text>
  </Box></InputProvider></LanguageContext.Provider>;
 }
 const root=renderSync(<Fixture width={120} height={24}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const frame=()=>{
  const screen=(instances.get(stdout) as any).frontFrame.screen;
  return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join(""));
 };
 try{
  for(const [width,height] of [[120,24],[72,16],[48,24],[120,36]]){
   stdout.columns=width;stdout.rows=height;stdout.emit("resize");root.rerender(<Fixture width={width} height={height}/>);await wait();await wait();
   for(let tab=0;tab<7;tab++){
    const labels=["经验","契约","治理","记忆","上下文","自定义动作","任务机制"];
    const lines=frame();const header=lines.findIndex(l=>l.includes(labels[tab]!));
    if(width>=48){
     expect(header).toBeGreaterThanOrEqual(0);
     if(width>=100)for(const label of labels)expect(lines[header]).toContain(label);
     else expect(lines[header]).toContain("...");
     expect(lines[header]).not.toContain("─");expect(lines[header+1]).toContain("─".repeat(width-4));
    }
    stdin.write("\x1b[C");await wait();
   }
   for(let n=0;n<7;n++){stdin.write("\x1b[D");await wait();}
  }
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);
