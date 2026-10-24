import React from "react";
import {test,expect} from "bun:test";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {PassThrough} from "node:stream";
import {Box,Text,renderSync,measureElement,type DOMElement} from "@anthropic/ink";
import {copy,definePage,pages,standardKey,moveSelection,reconcileSelection} from "./pageContract.js";
import {LanguageContext} from "./PageFrame.js";
import {InputProvider} from "../input/runtime.js";
import {StatsOverview} from "../components/StatsOverview.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {BtwPanel} from "../components/BtwPanel.js";
import type {BackendClient} from "../backend/client.js";
import type {ProcessInfo} from "../hooks/useLoopData.js";

const settle=()=>new Promise(r=>setTimeout(r,75));
function terminal(children:React.ReactNode){
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=100;stdout.rows=30;
 const stderr:any=new PassThrough();stderr.on("data",()=>{});let text="";
 stdout.on("data",(b:Buffer)=>{text+=b.toString("utf8");});
 const root=renderSync(<InputProvider>{children}</InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 return {stdin,text:()=>text,frame(){
  const screen=(instances.get(stdout) as any).frontFrame.screen;
  return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join("")).join(String.fromCharCode(10));
 },clear(){text="";},resize(children:React.ReactNode,cols:number,rows:number){stdout.columns=cols;stdout.rows=rows;root.rerender(<InputProvider>{children}</InputProvider>);},async send(value:string){stdin.write(value);await settle();},
  close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("page contract rejects missing translations and impossible header composition",()=>{
 expect(()=>copy({en:"Ready",zh:""},"zh")).toThrow("UI_TRANSLATION_MISSING");
 expect(()=>definePage({...pages.usage,elements:[{kind:"text",rich:false}]})).toThrow("header navigation requires");
 // @ts-expect-error New copy must supply both supported languages.
 const invalid:import("./pageContract.js").Copy={en:"Missing Chinese"};
 expect(invalid.en).toBe("Missing Chinese");
});
test("disabled items cannot be reconciled into focus or reached by navigation",()=>{
 const rows=[{id:0,enabled:true},{id:1,enabled:false},{id:2,enabled:true}];
 expect(moveSelection(rows,0,1)).toBe(2);expect(moveSelection(rows,2,-1)).toBe(0);
 expect(reconcileSelection(rows,1)).toBe(0);
 expect(moveSelection([{id:0,enabled:false}],0,1)).toBeUndefined();
 for(const input of ["r","R","a","k","x"])expect(standardKey(input,{})).toBeUndefined();
 expect(standardKey("",{return:true,paste:true})).toBeUndefined();
});
test("stats arrows select headers first, then content; Chinese uses the same routing",async()=>{
 const opened:string[]=[];
 const client:BackendClient={ready:true,close(){},async callTool(){return {summary:{},daily:[],projects:[{project:"one",task_count:1}],
  tasks:[{project:"one",task_id:"t1",created_at:"2026-10-23",duration_ms:1},{project:"two",task_id:"t2",created_at:"2026-10-23",duration_ms:1}]};}};
 const io=terminal(<LanguageContext.Provider value="zh"><StatsOverview client={client} cols={100} onDismiss={()=>{}} onInspect={p=>opened.push(p)}/></LanguageContext.Provider>);
 try{
  await settle();await io.send("\x1b[C"); // header selects tasks, not a day.
  expect(io.text()).toContain("按 Enter 打开选中项目");expect(opened).toHaveLength(0);
  await io.send("\r");await io.send("\x1b[B");await io.send("\r");
  expect(opened).toEqual(["two"]);
  await io.send("r");expect(opened).toHaveLength(1);
  expect(io.text()).toContain("用量总览");expect(io.text()).not.toContain("Usage overview");
 }finally{io.close();}
});
test("BTW clears submitted editor, pairs latest question with result and can browse parent",async()=>{
 const calls:any[]=[],browse:number[]=[];
 const client:BackendClient={ready:true,close(){},async callTool(operation,args){
  calls.push({operation,args});return operation==="runtime.btw"?{answer:"**Answer** "+args?.question,sidecar_id:"s"}:{status:"queued"};
 }};
 const process={process_id:"a",parent_id:null,created_at:"2026-10-23",actor_kind:"supervisor"} as ProcessInfo;
 const io=terminal(<Box height={14} flexDirection="column"><BtwPanel client={client} project="p" processId="a"
  question="Initial" answer="" sidecarId="s" processes={{a:process}} cols={100} onDismiss={()=>{}} onApply={()=>{}}
  onBrowseParent={n=>{browse.push(n);return n;}}/></Box>);
 try{
  await settle();await io.send("\r");await settle();
  await io.send("Follow-up question");await io.send("\r");await settle();
  const asks=calls.filter(c=>c.operation==="runtime.btw");expect(asks).toHaveLength(2);
  expect(asks[1].args.question).toBe("Follow-up question");
  await io.send("\t");await io.send("\t");await io.send("\t");await io.send("\x1b[5~");
  expect(browse).toEqual([-10]);
  await io.send("\t");await io.send("\t");await io.send("\t");
  await io.send("\x1b[C");await io.send("\x1b[C");await io.send("\r");
  expect(calls.some(c=>c.operation==="runtime.feedback")).toBe(false);await io.send("\r");
  const applied=calls.find(c=>c.operation==="runtime.feedback");
  expect(applied?.args.message).toContain("Question: Follow-up question");
  expect(applied?.args.message).not.toContain("Question: Initial");
 }finally{io.close();}
});

test("scoped drafts retain cursor and expanded paste through A/B/navigation switches",async()=>{
 let editor:ReturnType<typeof useTextInput>;
 function Probe({scope}:{scope:string}){editor=useTextInput("",scope);return <Text>{editor.value}</Text>;}
 const io=terminal(<Probe scope="A"/>);
 try{
  const paste="line of preserved data\n".repeat(20);
  editor!.insertText(paste,{paste:true});await settle();editor!.moveToStart();await settle();
  io.resize(<Probe scope="B"/>,100,30);await settle();
  expect(editor!.value).toBe("");editor!.insertText("B draft");await settle();
  io.resize(<Probe scope="navigation"/>,100,30);await settle();
  io.resize(<Probe scope="A"/>,100,30);await settle();
  expect(editor!.materialize()).toBe(paste);expect(editor!.cursor).toBe(0);
  io.resize(<Probe scope="B"/>,100,30);await settle();expect(editor!.value).toBe("B draft");
 }finally{io.close();}
});
test("BTW starts without a scope gate, scrolls long answers after resize, and has bounded height",async()=>{
 const calls:any[]=[],browse:number[]=[];let drawer:DOMElement|null=null;
 const client:BackendClient={ready:true,close(){},async callTool(operation,args){
  calls.push({operation,args});return {answer:Array.from({length:45},(_,i)=>"row "+i+" long explanation 中文").join("\n\n"),sidecar_id:"s"};
 }};
 const a={process_id:"a",parent_id:null,created_at:"1",actor_kind:"supervisor"} as ProcessInfo;
 const b={...a,process_id:"b",parent_id:"a",session_id:"bs",display_name:"B"} as ProcessInfo;
 const fixture=(width:number,height:number)=><Box width={width} height={height} flexDirection="column">
  <Text>Parent stays visible</Text><Box ref={node=>{drawer=node;}} maxHeight={Math.floor(height/2)} flexDirection="column" flexShrink={0}>
  <BtwPanel client={client} project="p" processId="b" question="Start now" answer="" sidecarId="s" processes={{a,b}}
    cols={width} maxBodyRows={Math.max(2,Math.floor(height/2)-6)} onDismiss={()=>{}} onApply={()=>{}} onBrowseParent={n=>{browse.push(n);return n;}}/>
  </Box></Box>;
 const io=terminal(fixture(100,30));
 try{
  await settle();await settle();expect(calls).toHaveLength(1);
  expect(calls[0].args.process_ids).toEqual(["b","a"]);
  for(const [width,height] of [[44,24],[120,40],[60,28]]){
   io.resize(fixture(width,height),width,height);await settle();await settle();
   expect(measureElement(drawer!).height).toBeLessThanOrEqual(Math.floor(height/2));
   io.clear();for(let n=0;n<6;n++)await io.send("\x1b[6~");
   expect(io.frame()).toContain("row 44");
   await io.send("\x1b[5~");await io.send("\t");await io.send("\t");await io.send("\t");
   await io.send("\x1b[A");expect(browse.at(-1)).toBe(-1);
   await io.send("\t");
  }
 }finally{io.close();}
});
test("heatmap arrows move one grid axis and header owns its divider",async()=>{
 const client:BackendClient={ready:true,close(){},async callTool(){return {summary:{},daily:[],projects:[],tasks:[]};}};
 const io=terminal(<StatsOverview client={client} cols={100} onDismiss={()=>{}}/>);
 try{
  await settle();await io.send("\r");await io.send("\x1b[D");
  io.clear();await io.send("\x1b[C");
  const now=new Date(),today=String(now.getFullYear())+"-"+String(now.getMonth()+1).padStart(2,"0")+"-"+String(now.getDate()).padStart(2,"0");
  const dates=[...io.text().matchAll(/(20[0-9]{2}-[0-9]{2}-[0-9]{2}) · [0-9,]+ tokens/g)];
  expect(dates.at(-1)?.[1]).toBe(today);expect(io.text()).toContain("─");
 }finally{io.close();}
});
