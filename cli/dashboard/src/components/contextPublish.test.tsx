import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {RuntimeContextPanel,contextSessions} from "./RuntimeContextPanel.js";
import {PublishPanel} from "./PublishPanel.js";
const wait=()=>new Promise(r=>setTimeout(r,80));
function mount(children:React.ReactNode){
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=true;stdout.columns=120;stdout.rows=24;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const root=renderSync(<InputProvider>{children}</InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 return {async send(key:string){stdin.write(key);await wait();},
  resize(child:React.ReactNode,width:number){stdout.columns=width;stdout.emit("resize");root.rerender(<InputProvider>{child}</InputProvider>);},
  lines(){const screen=(instances.get(stdout) as any).frontFrame.screen;return Array.from({length:screen.height},(_,y)=>
   Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join(""));},
  close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("context count and compact process choices remain grouped, unique and selectable after resize",async()=>{
 const a:any={process_id:"a",actor_kind:"supervisor",created_at:"1",model_id:"MODEL-A",context:{limit:10000,used_tokens:300}};
 const b:any={...a,process_id:"b",actor_kind:"worker",parent_id:"a",created_at:"2",display_name:"Long worker 中文名称",model_id:"MODEL-B"};
 const fixture=(width:number)=><LanguageContext.Provider value="zh"><Box width={width} height={24} flexDirection="column">
  <RuntimeContextPanel project="demo" processes={{a,b}} onDismiss={()=>{}}/>
 </Box></LanguageContext.Provider>;
 const io=mount(fixture(120));
 try{
  await wait();
  for(const width of [120,72,40,28,120]){
   io.resize(fixture(width),width);await wait();await wait();
   const lines=io.lines(),all=lines.join(" ");
   expect(all.match(/主进程/g)?.length).toBe(1);
   const row=lines.find(line=>line.includes("1/2"))!;
   expect(row).toContain("主进程");expect(row.indexOf("主进程")).toBeLessThan(16);
   await io.send("\x1b[5~");
   expect(io.lines().join(" ")).toContain("MODEL-A");
   await io.send("\x1b[C");expect(io.lines().join(" ")).toContain("MODEL-B");
   expect(io.lines().join(" ")).toContain("2/2");
   await io.send("\x1b[D");
  }
 }finally{io.close();}
});
test("publish translates tabs, states, footer and local validation without changing operations",async()=>{
 const calls:any[]=[];let footer:any;
 const client:any={ready:true,close(){},async callTool(operation:string,args:any){
  calls.push({operation,args});if(operation==="publish.get")return {commit_format:{prefix:"ANBM",number_start:0,padding:false,plugins:[],template_name:"default"},
   privacy:{enabled:true,deep_scan:false,content_level:2},remote_url:"",stages:{}};
  if(operation==="history.list")return [];throw Error("Unexpected mutation");
 }};
 function Fixture(){const input=useTextInput();return <LanguageContext.Provider value="zh">
  <PublishPanel client={client} project="p" cmdInput={input} onFooter={value=>{footer=value;}} onDismiss={()=>{}} onOpenTrial={()=>{}} onOpenFormal={()=>{}}/>
 </LanguageContext.Provider>;}
 const io=mount(<Fixture/>);
 try{
  await wait();const initial=io.lines().join(" ");
  expect(initial).toContain("提交格式");expect(initial).toContain("起始编号");expect(initial).not.toContain("Commit Format");
  await io.send("\x1b[B");await io.send("\r");expect(footer.statusText).toContain("保存");
  await io.send("\x7f");await io.send("-1");await io.send("\r");
  expect(io.lines().join(" ")).toContain("起始编号必须为非负整数");await io.send("\x1b");
  await io.send("\x1b[C");expect(io.lines().join(" ")).toContain("隐私扫描");expect(io.lines().join(" ")).toContain("开启");
  await io.send("\x1b[C");expect(io.lines().join(" ")).toContain("未配置发布仓库");
  expect(calls.some(c=>c.operation==="publish.set")).toBe(false);
 }finally{io.close();}
});

test("context follows the latest execution of each durable A/B conversation",()=>{
 const old:any={process_id:"old-a",session_id:"a",actor_kind:"supervisor",created_at:"1"};
 const next:any={...old,process_id:"new-a",created_at:"3"};
 const child:any={process_id:"b",session_id:"b",actor_kind:"worker",parent_id:"old-a",created_at:"2"};
 expect(contextSessions([old,child,next]).map(p=>p.process_id)).toEqual(["new-a","b"]);
});

test("project check settings edit targets, seeds and limits without starting checks",async()=>{
 const calls:any[]=[];let checks:any[]=[];
 const client:any={async callTool(operation:string,args:any){
  calls.push({operation,args});
  if(operation==="publish.get")return {commit_format:{},privacy:{},remote_url:"",checks};
  if(operation==="history.list")return [];
  if(operation==="publish.set"){checks=args.value;return {ok:true};}
  throw Error("Unexpected execution");
 }};
 function Fixture(){const input=useTextInput();return <LanguageContext.Provider value="zh">
  <PublishPanel client={client} project="p" cmdInput={input} onFooter={()=>{}} onDismiss={()=>{}} onOpenTrial={()=>{}} onOpenFormal={()=>{}}/>
 </LanguageContext.Provider>;}
 const io=mount(<Fixture/>);
 try{
  await wait();for(let i=0;i<3;i++)await io.send("\x1b[C");
  expect(io.lines().join(" ")).toContain("必需测试");
  await io.send("\r");await io.send("tests/a.py | 42,7 | 20; tests/b.py | 11 | 30");await io.send("\r");await wait();
  expect(calls.find(c=>c.operation==="publish.set").args).toEqual({project:"p",section:"checks",value:[
   {target:"tests/a.py",seeds:[42,7],timeout:20},{target:"tests/b.py",seeds:[11],timeout:30}]});
  expect(io.lines().join(" ")).toContain("tests/a.py");
  expect(calls.some(c=>c.operation==="publish.verify")).toBe(false);
 }finally{io.close();}
});
