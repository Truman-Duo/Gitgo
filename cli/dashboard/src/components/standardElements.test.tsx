import React from "react";
import {test, expect} from "bun:test";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {displayWidth} from "../theme/index.js";
import {FormFieldRow, fieldLabelWidth} from "./FormFieldRow.js";
import {SelectionText} from "./Selection.js";
import {UpdatePanel} from "./UpdatePanel.js";
import {BinWorkspace} from "./BinWorkspace.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {TaskMechanismsPanel} from "./TaskMechanismsPanel.js";
import {DecisionPanel} from "./DecisionPanel.js";

const wait=(ms=85)=>new Promise(resolve=>setTimeout(resolve,ms));
// Production Ink rendering and input routing; no visible-terminal QA claim.
function mount(draw: (width:number,height:number)=>React.ReactNode,width:number,height:number) {
  const previous=chalk.level;chalk.level=3;
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:width,rows:height});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const view=()=> <InputProvider><Box width={stdout.columns} height={stdout.rows} flexDirection="column">{draw(stdout.columns,stdout.rows)}</Box></InputProvider>;
  const root=renderSync(view(),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
  const line=(y:number)=>Array.from({length:ink.frontFrame.screen.width},(_,x)=>cellAt(ink.frontFrame.screen,x,y)?.char||"").join("");
  return {line, async send(keys:string){stdin.write(keys);await wait();},
    async resize(w:number,h:number){stdout.columns=w;stdout.rows=h;stdout.emit("resize");root.rerender(view());await wait();},
    locate(text:string){for(let y=0;y<ink.frontFrame.screen.height;y++){const row=line(y),at=row.indexOf(text);if(at>=0)return {x:displayWidth(row.slice(0,at)),y};}throw Error("Missing "+text+"\n"+Array.from({length:ink.frontFrame.screen.height},(_,y)=>line(y)).join("\n"));},
    styles(at:{x:number;y:number}){return ink.stylePool.get(cellAt(ink.frontFrame.screen,at.x,at.y)!.styleId).map((s:any)=>s.code) as string[];},
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;}};
}

for(const language of ["en","zh"] as const)test(`blocked extension reconciliation uses exact instance CAS and keeps failed evidence visible (${language})`,async()=>{
 const extension:any={id:"recoverable",phase:"blocked",epoch:1,revision:7,definition_revision:2,version_policy:"pinned",
   error:{code:"MECHANISM_INTERRUPTED_INVOCATION",effect_state:"unknown"}};
 const process:any={process_id:"root",active_task_id:"actual-task",created_at:"2026-10-31",actor_kind:"supervisor",status:"awaiting_user",harness_mechanisms:[extension]};
 let writes=0;
 const client:any={async callTool(operation:string,args:any){
   expect(operation).toBe("runtime.mechanism");
   if(args.action==="reconcile"){
     writes++;expect(args).toMatchObject({project:"fixture",process_id:"root",task_id:"actual-task",instance_id:"recoverable",expected_revision:7});
     return {reconciled:false,reason:"canonical_evidence_missing",instance:extension};
   }
   return {state:{revision:0},extensions:[extension]};
 }};
 const io=mount(()=> <LanguageContext.Provider value={language}><TaskMechanismsPanel client={client} project="fixture"
   processes={{root:process}} onDismiss={()=>{}}/></LanguageContext.Provider>,100,30);
 try{
   await wait();await io.send("\r");expect(io.locate(language==="zh"?"核对恢复":"Reconcile")).toBeDefined();
   await io.send("\r");expect(writes).toBe(0);
   await io.send("\r");await wait();expect(writes).toBe(1);
   expect(io.locate("canonical_evidence_missing")).toBeDefined();
   expect(io.locate(language==="zh"?"受阻":"Blocked")).toBeDefined();
 }finally{io.close();}
});

for(const language of ["en","zh"] as const)test(`mechanism detail preserves rejected, withdrawn and blocked status semantics (${language})`,async()=>{
 const process:any={process_id:"root",created_at:"2026-10-30",actor_kind:"supervisor",status:"running",task_maintenance:{revision:1,epoch:1}};
 const response={state:{phase:"ready",revision:1,epoch:1,
  changes:{one:{id:"one",status:"rejected",content:"one"},two:{id:"two",status:"withdrawn",content:"two"}},
  maintenance_items:{three:{id:"three",status:"blocked",source:"component"}},related:{candidates:{four:{id:"four",status:"discarded",work:"four"}}}},
  extensions:[{id:"extension",phase:"pending_activation",epoch:1,revision:2,definition_revision:2,version_policy:"pinned"}]};
 const client:any={async callTool(){return response;}};
 const io=mount(()=> <LanguageContext.Provider value={language}><TaskMechanismsPanel client={client} project="fixture"
  processes={{root:process}} onDismiss={()=>{}}/></LanguageContext.Provider>,100,38);
 try{await wait();await io.send("\r");
  for(const text of language==="zh"?["已拒绝","已撤回","受阻","已丢弃","待生效","固定版本"]:["Rejected","Withdrawn","Blocked","Discarded","Pending activation","Pinned version"])
   expect(io.locate(text)).toBeDefined();
 }finally{io.close();}
});

for(const language of ["en","zh"] as const)test(`question materials remain evidence beside a genuine unanswered runtime checkpoint (${language})`,async()=>{
 const decision:any={task_id:"task",process_id:"process",decision_id:"actual-card",kind:"runtime_checkpoint",question:"Predict output",
  why_user_must_decide:"Requested checkpoint",options:[{label:"Hint"},{label:"Skip"},{label:"Exit"}],allow_free_form:true,
  materials:[{reference:"sha256:fixture",summary:"Immutable source material",source:"immutable_discussion",authority_granted:false}]};
 const io=mount(()=> <LanguageContext.Provider value={language}><DecisionPanel decision={decision} selectedIndex={0}
  composing={false} submitting={false}/></LanguageContext.Provider>,90,25);
 try{await wait();expect(io.locate(language==="zh"?"补充材料":"Supplementary material")).toBeDefined();
  expect(io.locate("Immutable source material")).toBeDefined();expect(io.locate("Predict output")).toBeDefined();
  expect(io.styles(io.locate("Hint"))).toContain("\x1b[1m");
 }finally{io.close();}
});

for(const language of ["en","zh"] as const)test(`mechanism review stage follows canonical status updates without manual refresh (${language})`,async()=>{
 let phase="reviewing",calls=0;
 const completion=()=>({return_counters:{stage:2},reviews:[{reviewer_process_id:"reviewer-real-fixture",epoch:1,phase,
  verdict:phase==="complete"?"changes_required":undefined}]});
 const process:any={process_id:"root",created_at:"2026-10-30",actor_kind:"supervisor",status:"running",
  harness_mechanisms:[{id:"extension",phase:"ready",epoch:1,revision:2,completion:completion()}]};
 const client:any={async callTool(){calls++;return {state:{revision:0},extensions:[{...process.harness_mechanisms[0],completion:completion()}]};}};
 const io=mount(()=> <LanguageContext.Provider value={language}><TaskMechanismsPanel client={client} project="fixture"
  processes={{root:process}} onDismiss={()=>{}}/></LanguageContext.Provider>,100,30);
 try{await wait();await io.send("\r");expect(io.locate(language==="zh"?"审阅中":"Reviewing")).toBeDefined();
  expect(io.locate(language==="zh"?"返回执行次数：2":"Returns to execution: 2")).toBeDefined();
  const previous=calls;phase="complete";process.harness_mechanisms[0].completion=completion();await io.resize(100,30);await wait();
  expect(calls).toBeGreaterThan(previous);expect(io.locate(language==="zh"?"需要修改":"Changes required")).toBeDefined();
 }finally{io.close();}
});

for(const language of ["en","zh"] as const)test(`fields retain cell spacing and weight with long labels at narrow widths (${language})`,async()=>{
  const labels=language==="zh"?["特别长的中文设置字段名称","短字段"]:["A particularly long setting name","Short"];
  const io=mount(width=><LanguageContext.Provider value={language}>{labels.map((label,index)=><FormFieldRow key={index}
    label={label} suffix="" selected={index===0} labelWidth={fieldLabelWidth(labels,Math.max(1,width-8),"")} highlight={false}>
    <SelectionText wrap="truncate-end">{index===0?"ON":"OFF"}</SelectionText>
  </FormFieldRow>)}</LanguageContext.Provider>,80,6);
  try{await wait();for(const width of [80,40,18,4]){
    await io.resize(width,6);
    if(width>4){expect(io.locate("ON").x).toBe(io.locate("OFF").x);
      expect(io.styles(io.locate("ON"))).toContain("\x1b[1m");expect(io.styles(io.locate("OFF"))).not.toContain("\x1b[1m");}
    for(let y=0;y<2;y++){expect(displayWidth(io.line(y))).toBeLessThanOrEqual(width);expect(io.line(y)).not.toMatch(/[›▸█]/u);}
  }}finally{io.close();}
});

test("Update uses standard field colors, label-only blink and bounded selection through resize",async()=>{
  const calls:string[]=[];let writes=0;
  const preferences={schema_version:1,revision:3,channel:"preview",automatic_check:true,automatic_download:false,automatic_install:false};
  const client:any={async callTool(operation:string){calls.push(operation);if(operation==="update.configure")writes++;
    return {schema_version:6,product_id:"gitgo-terminal",version:"1.0.0-alpha.3",status:"idle",channel:"preview",enabled:false,can_apply:false,latest_version:null,
      automatic_check:true,preferences,automatic_install_available:true,can_check:true,can_download:false,can_request_apply:false,
      can_request_rollback:false,rollback:null,candidate:null,pending_restart:null,apply_strategy:"immediate",blockers:[],
      automatic:{state:"not_checked",last_attempt:null,next_attempt:null,error_code:null}};}};
  const io=mount(width=><LanguageContext.Provider value="zh"><UpdatePanel client={client} cols={width} language="zh" onDismiss={()=>{}}/></LanguageContext.Provider>,64,16);
  try{
    await wait();const label=io.locate("更新渠道"),value=io.locate("预览版");
    expect(io.styles(label)).toContain("\x1b[1m");expect(io.styles(label).some(c=>c.includes("[48;"))).toBe(false);
    expect(io.locate("开启").x).toBe(value.x);
    await io.send("\x1b[B".repeat(500));expect(io.styles(io.locate("刷新本地状态"))).toContain("\x1b[48;2;42;42;42m");
    await io.resize(40,9);expect(io.locate("刷新本地状态").y).toBeLessThan(8);
    await io.send("\x1b[A".repeat(500));expect(io.styles(io.locate("更新渠道"))).toContain("\x1b[1m");
    await io.resize(64,16);await io.send("\r");const draftValue=io.locate("预览版");
    await wait(450);expect(io.line(io.locate("预览版").y)).not.toContain("更新渠道");
    expect(io.locate("预览版").x).toBe(draftValue.x);expect(writes).toBe(0);
    await io.send("\x1b[C");expect(io.line(draftValue.y)).toContain("正式版");expect(writes).toBe(0);
    await io.send("\x1b");expect(io.line(draftValue.y)).toContain("预览版");expect(io.line(draftValue.y)).toContain("更新渠道");
    expect(calls).toEqual(["update.status"]);
    for(let y=0;y<16;y++)expect(io.line(y)).not.toContain("›");
  }finally{io.close();}
});

test("Bin keeps its last valid row visible on short screens and navigation never mutates",async()=>{
  const calls:string[]=[];
  const client:any={async callTool(operation:string){calls.push(operation);
    if(operation==="bin.targets")return {projects:Array.from({length:35},(_,i)=>({name:`project-${i}`,project:`project-${i}`})),processes:[]};
    if(operation==="deletion.list")return [];
    return {};
  }};
  function Fixture(){const input=useTextInput();return <LanguageContext.Provider value="zh"><BinWorkspace client={client} cmdInput={input} onFooter={()=>{}} onDismiss={()=>{}}/></LanguageContext.Provider>;}
  const io=mount(()=> <Fixture/>,64,12);
  try{
    await wait();await io.send("\x1b[B".repeat(500));expect(io.styles(io.locate("project-34"))).toContain("\x1b[1m");
    await io.resize(40,8);expect(io.locate("project-34").y).toBeLessThan(7);
    await io.send("\x1b[A".repeat(500));expect(io.styles(io.locate("project-0"))).toContain("\x1b[1m");
    await io.send("\x1b[C");expect(io.locate("此处暂无归档记录").y).toBeLessThan(8);
    await io.send("\x1b[C");await io.send("\x1b[B".repeat(500));
    expect(io.locate("工作区缺失后归档").y).toBeLessThan(7);
    expect(new Set(calls)).toEqual(new Set(["bin.targets","deletion.list","config.get"]));
  }finally{io.close();}
});
