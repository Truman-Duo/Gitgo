import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,Text,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputLayer,InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {HelpPanel} from "./HelpPanel.js";
import {helpGuide,helpKeys} from "../ui/helpContent.js";
import {copy} from "../ui/pageContract.js";
import {applicationBindings} from "../input/router.js";
import type {Scene} from "../state/store.js";
const wait=()=>new Promise(r=>setTimeout(r,85));

test("Help owns two tabs, scrolls without moving the header, resizes and closes in two stages",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=true;stdout.columns=100;stdout.rows=24;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 let closed=0;
 function Fixture({width,height,scene="workspace",language="zh"}:{width:number;height:number;scene?:Scene;language?:"en"|"zh"}){
  return <LanguageContext.Provider value={language}><InputProvider><InputLayer priority={100}>
   <Box width={width} height={height} flexDirection="column"><HelpPanel scene={scene} onDismiss={()=>closed++}/><Text>STATUS</Text></Box>
  </InputLayer></InputProvider></LanguageContext.Provider>;
 }
 const root=renderSync(<Fixture width={100} height={24}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const frame=()=>{
  const screen=(instances.get(stdout) as any).frontFrame.screen;
  return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join(""));
 };
 try{
  await wait();
  expect(frame().join("\n")).toContain("向主进程描述目标");
  stdin.write("\x1b[C");await wait();
  expect(frame().join("\n")).toContain("打开命令推荐");
  expect(frame().join("\n")).not.toContain("向主进程描述目标");
  for(const [width,height] of [[46,14],[72,18],[120,30]]){
   stdout.columns=width;stdout.rows=height;stdout.emit("resize");
   root.rerender(<Fixture width={width} height={height}/>);await wait();await wait();
   const rows=frame(),header=rows.findIndex(x=>x.includes("使用说明")&&x.includes("按键说明"));
   expect(header).toBeGreaterThanOrEqual(0);expect(rows[header+1]).toContain("─".repeat(width-4));
   expect(rows.join("\n")).toContain("STATUS");
   stdin.write("\x1b[6~");await wait();
   expect(frame()[header]).toContain("按键说明");
   stdin.write("\x1b[5~");await wait();
  }
  stdin.write("\x1b[D");await wait();
  expect(frame().join("\n")).toContain("向主进程描述目标");
  stdin.write("\x1b[B");await wait();
  stdin.write("\x1b");await wait();expect(closed).toBe(0);
  stdin.write("\x1b");await wait();expect(closed).toBe(1);
  root.rerender(<Fixture width={120} height={30} scene="projects" language="en"/>);await wait();
  expect(frame().join("\n")).toContain("Open a project workspace");
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);
test("guidance covers each real scene and key labels follow configured bindings",()=>{
 for(const language of ["zh","en"] as const)for(const scene of ["projects","workspace","process_list","agent_detail"] as const){
  const t=(value:any)=>copy(value,language),guide=helpGuide(scene,t);
  expect(guide.steps.length).toBeGreaterThan(2);expect(guide.purpose.length).toBeGreaterThan(10);
  if(language==="zh")expect(guide.title).toMatch(/[\u3400-\u9fff]/);
 }
 const before=applicationBindings.snapshot();
 try{
  applicationBindings.configure({version:1,overrides:{"editor.external":{key:"j",ctrl:true}}});
  const rows=helpKeys("workspace",v=>v.en);
  expect(rows.some(row=>row.keys==="Ctrl+J")).toBe(true);
  expect(helpGuide("workspace",v=>v.en).steps.join(" ")).toContain("Ctrl+J");
 }finally{applicationBindings.configure(before);}
});
