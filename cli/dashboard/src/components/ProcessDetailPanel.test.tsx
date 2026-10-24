import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,Text,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputLayer,InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {ProcessDetailPanel,processDetailSections} from "./ProcessDetailPanel.js";
import {Overview} from "./Overview.js";
import {ProcessList} from "./ProcessList.js";
import {copy} from "../ui/pageContract.js";
const t=(v:any)=>copy(v,"zh");
const child=(id:string,path:string,status="running")=>({process_id:id,status,display_name:"same name",actor_kind:"worker",role:"executor",created_at:"1",
 parent_id:"main",parent_ids:["main"],depends_on:["prerequisite"],downstream_ids:["next"],child_ids:["nested"],
 model_id:"model-"+id,steps_used:0,max_steps:50,ring_level:3,worktree_path:path,worktree:{path,isolated:false,state:"shared_workspace"}} as any);
const processes:any={main:{process_id:"main",display_name:"A"},one:child("one","C:/one"),two:child("two","C:/two"),
 prerequisite:{process_id:"prerequisite",display_name:"Before"},next:{process_id:"next",display_name:"After"},nested:{process_id:"nested",display_name:"Nested",parent_id:"one"}};
test("detail retains budgets, full paths and all actual relationship types",()=>{
 const sections=processDetailSections(processes.one,processes,t);
 const rows=sections.flatMap(s=>s.rows);
 expect(rows.find(r=>r.label==="位置")?.value).toBe("C:/one");
 expect(rows.find(r=>r.label==="已用步数 / 当前执行上限")?.value).toBe("0 / 50");
 for(const value of ["A · main","Before · prerequisite","After · next","Nested · nested"])expect(rows.some(r=>r.value===value)).toBe(true);
 expect(rows.find(r=>r.label==="任务时限（秒）")?.value).toBe("未记录");
});
const wait=()=>new Promise(r=>setTimeout(r,80));
function io(){
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=true;stdout.columns=84;stdout.rows=24;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 return {stdin,stdout,stderr};
}
function frame(stdout:any){
 const screen=(instances.get(stdout) as any).frontFrame.screen;
 return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||"").join(""));
}
test("detail keeps its target while polling changes other rows, scrolls, resizes and returns",async()=>{
 const streams=io();let closed=0;
 function Fixture({width=84,height=24,rows=processes,id="one"}:{width?:number;height?:number;rows?:any;id?:string}){
  return <LanguageContext.Provider value="zh"><InputProvider><InputLayer priority={100}><Box width={width} height={height} flexDirection="column">
   <ProcessDetailPanel project="demo" processId={id} processes={rows} onDismiss={()=>closed++}/><Text>STATUS</Text>
  </Box></InputLayer></InputProvider></LanguageContext.Provider>;
 }
 const root=renderSync(<Fixture/>,{...streams,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();expect(frame(streams.stdout).join("\n")).toContain("model-one");
  root.rerender(<Fixture rows={{...processes,one:{...processes.one,status:"awaiting_user"},two:{...processes.two,model_id:"never-select-me",created_at:"9"}}}/>);
  await wait();expect(frame(streams.stdout).join("\n")).toContain("等待用户");
  expect(frame(streams.stdout).join("\n")).not.toContain("never-select-me");
  streams.stdin.write("\x1b[6~");await wait();
  const before=frame(streams.stdout).join("\n");
  expect(before).toContain("子进程详情");
  const viewport=(node:any):any=>node.scrollViewportHeight!==undefined?node:(node.childNodes||[]).map(viewport).find(Boolean);
  expect(viewport((instances.get(streams.stdout) as any).rootNode).scrollTop).toBeGreaterThan(0);
  for(const [width,height] of [[42,14],[120,32],[68,18]]){
   streams.stdout.columns=width;streams.stdout.rows=height;streams.stdout.emit("resize");
   root.rerender(<Fixture width={width} height={height}/>);await wait();await wait();
   expect(frame(streams.stdout)[1]).toContain("子进程详情");expect(frame(streams.stdout).join("\n")).toContain("STATUS");
  }
  streams.stdin.write("\x1b");await wait();expect(closed).toBe(1);
  root.rerender(<Fixture id="gone"/>);await wait();
  expect(frame(streams.stdout).join("\n")).toContain("此子进程已不可用");
  expect(frame(streams.stdout).join("\n")).not.toContain("model-two");
 }finally{root.unmount();root.cleanup();Object.values(streams).forEach((s:any)=>s.destroy());}
},12000);
test("project table keeps locations and uses Chinese headings; subprocess list no longer leaks detail rows",async()=>{
 const streams=io();let status="";
 const project:any={name:"project-name",workspace:"C:/Users/owner/location-folder",activeProcessCount:2,daemonOnline:true};
 const root=renderSync(<Overview projects={[project]} sel={0} mode="COMMAND" cols={84} listActive language="zh"/>,
  {...streams,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();const lines=frame(streams.stdout);
  expect(lines[1]).toContain("项目名称");expect(lines[1]).toContain("项目位置");expect(lines[1]).toContain("进程");
  expect(lines.join("\n")).toContain("location-folder");
  const rows={one:processes.one,two:child("two","C:/two","awaiting_user")};
  root.rerender(<LanguageContext.Provider value="zh"><InputProvider><Box height={20} width={84}>
   <ProcessList project="demo" loopData={{processes:rows,recoveryAvailable:[],storage:null} as any}
    cols={84} selIdx={0} onStatusUpdate={v=>{status=v;}}/>
  </Box></InputProvider></LanguageContext.Provider>);
  await wait();const text=frame(streams.stdout).join("\n");
  expect(text).toContain("子进程");expect(text).not.toContain("C:/one");expect(text).not.toContain("model-one");
  expect(text).not.toContain("Selected subprocess");expect(text).not.toContain("PgUp");
  expect(status).toContain("1 个工作中");expect(status).toContain("1 个待答复");
 }finally{root.unmount();root.cleanup();Object.values(streams).forEach((s:any)=>s.destroy());}
});
