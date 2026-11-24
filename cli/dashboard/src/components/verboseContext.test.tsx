import React,{useMemo} from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {StreamingMessage} from "./StreamingMessage.js";
import {MessageRow} from "./MessageRow.js";
import {TurnTimeline} from "./TurnTimeline.js";
import {GeneralTab} from "./config/GeneralTab.js";
import {InputProvider} from "../input/runtime.js";
import {useGeneralConfig} from "../hooks/useGeneralConfig.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {initStreamState,reduceStreamEvent} from "../daemon/streamReducer.js";
import {traceBodyReader} from "../chat/traceSource.js";
import type {StreamActivity} from "../types.js";

const pause=(ms=20)=>new Promise(resolve=>setTimeout(resolve,ms));
const text=(node:any):string=>node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
const nodes=(node:any):any[]=>[node,...(node.childNodes ?? []).flatMap(nodes)];
async function until(condition:()=>boolean){for(let i=0;i<120;i++){if(condition())return;await pause();}expect(condition()).toBe(true);}
function mount(element:React.ReactNode){
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:48});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const root=renderSync(<Box width={100} height={48} flexDirection="column">{element}</Box>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const tree=()=>(instances.get(stdout) as any).rootNode;
  return {stdin,tree,text:()=>text(tree()),rerender:(value:React.ReactNode)=>root.rerender(<Box width={100} height={48} flexDirection="column">{value}</Box>),
    click(label:string){const node=nodes(tree()).find(node=>node._eventHandlers?.onClick&&text(node).includes(label));expect(node).toBeDefined();node._eventHandlers.onClick({});},
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
const saved={representation:"canonical_provider_input",input_provenance:{schema_version:1,server_confirmed:false,sources:[
  {message_index:0,message_type:"project_rules",content_sha256:"saved-digest",sections:[{name:"AGENTS.md",provenance:"project",version:"SAVED V1"}]},
]},messages:[{content:"SAVED ORIGINAL RULES\n"+"长".repeat(80)+" EXACT SOURCE TAIL"}]};
function inputView(args:any){
  if(args.view==="input_sources")return {representation:"canonical_provider_input_sources",input_provenance:saved.input_provenance};
  expect(args.view).toBe("input_source");
  const points=[...saved.messages[0]!.content],offset=args.offset || 0,end=Math.min(points.length,offset+8192);
  return {representation:"canonical_provider_input_source_page",source_index:args.source_index,offset,next_offset:end,total_chars:points.length,
    has_more:end<points.length,text:points.slice(offset,end).join(""),before:points.slice(Math.max(0,offset-64),offset).join(""),after:points.slice(end,end+64).join("")};
}

test("saving verbose changes the current stream and existing history immediately, without another request",async()=>{
  let verbose=false;const operations:string[]=[];let detailReads=0;
  const client:any={ready:true,close(){},callTool:async(name:string,args:any={})=>{
    operations.push(name);
    if(name==="config.get")return {verbose,language:"en",version:"fixture"};
    if(name==="config.set"){verbose=Boolean(args.value);return {ok:true};}
    if(name==="product.version")return {version:"fixture"};
    if(name==="config.terminals")return {options:[],warnings:[]};
    if(name==="runtime.trace" && args.action==="detail"){detailReads++;return {detail:inputView(args)};}
    throw new Error("Unexpected operation: "+name);
  }};
  const start=Date.now();
  let state=reduceStreamEvent(initStreamState(),{event:"provider_request_started",process_id:"a",seq:1,model:"saved-model",
    time:new Date(start-2000).toISOString(),detail_ref:"saved"});
  state=reduceStreamEvent(state,{event:"reasoning_delta",process_id:"a",delta:"CURRENT PRIVATE THOUGHT"});
  const stream={...state,timestamp:new Date(start-120000).toISOString()};
  const history:StreamActivity[]=[{kind:"context",request:{event:"provider_request_started",seq:2,model:"saved-model",detail_ref:"history"},record_id:"history-input"}];
  function Fixture(){
    const config=useGeneralConfig(client),input=useTextInput(),read=useMemo(()=>traceBodyReader(client,"fixture",""),[]);
    return <>
      <Box height={22}><GeneralTab client={client} project="fixture" cmdInput={input} onFooter={()=>{}} report={()=>{}} contentFocused
        shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></Box>
      <Box height={12}><StreamingMessage streaming={stream} contentWidth={96} client={client} project="fixture" verbose={config.verbose}/></Box>
      <Box height={8}><TurnTimeline activity={history} tools={[]} notices={[]} verbose={config.verbose} width={96} readBody={read}/></Box>
    </>;
  }
  const io=mount(<InputProvider><Fixture/></InputProvider>);
  const countContexts=()=>nodes(io.tree()).filter(node=>node._eventHandlers?.onClick&&text(node).includes("Injected context")).length;
  const thinkingButtons=()=>nodes(io.tree()).filter(node=>node._eventHandlers?.onClick&&/Thinking/.test(text(node))).length;
  async function toggle(){io.stdin.write("\r");await pause(40);io.stdin.write("\x1b[C");await pause(40);io.stdin.write("\r");}
  try{
    await until(()=>io.text().includes("Thinking"));
    expect(countContexts()).toBe(0);expect(thinkingButtons()).toBe(0);expect(detailReads).toBe(0);
    expect(io.text()).not.toContain("CURRENT PRIVATE THOUGHT");
    await toggle();await until(()=>countContexts()===2);
    expect(verbose).toBe(true);expect(io.text()).toContain("CURRENT PRIVATE THOUGHT");
    const seconds=Number(io.text().match(/Thinking… (\d+(?:\.\d+)?)s/)?.[1]);
    expect(seconds).toBeGreaterThanOrEqual(2);expect(seconds).toBeLessThan(20);
    expect(detailReads).toBe(0);
    io.click("Injected context");await until(()=>io.text().includes("AGENTS.md"));
    io.click("AGENTS.md");await until(()=>io.text().includes("EXACT SOURCE TAIL"));
    expect(io.text()).toContain("SAVED V1");expect(detailReads).toBe(2);
    await toggle();await until(()=>countContexts()===0 && !io.text().includes("CURRENT PRIVATE THOUGHT"));
    expect(verbose).toBe(false);expect(thinkingButtons()).toBe(0);
    expect(io.text()).not.toContain("EXACT SOURCE TAIL");expect(io.text()).toContain("Thinking");
    await toggle();await until(()=>countContexts()===2);
    expect(detailReads).toBe(2);expect(io.text()).not.toContain("EXACT SOURCE TAIL");
    expect(operations.every(name=>["config.get","config.set","config.terminals","product.version","runtime.trace"].includes(name))).toBe(true);
  }finally{io.close();}
},10000);

test("injection is an independent verbose record and never creates a thinking stage",async()=>{
  const activity:StreamActivity[]=[{kind:"context",request:{event:"provider_request_parameters",detail_ref:"saved"}}];
  const draw=(verbose:boolean)=><TurnTimeline activity={activity} tools={[]} notices={[]} width={96} active verbose={verbose}/>;
  const io=mount(draw(true));
  try{
    await pause(60);expect(io.text()).toContain("Injected context");expect(io.text()).not.toContain("Thinking");
    io.rerender(draw(false));await pause(40);
    expect(io.text()).not.toContain("Injected context");expect(io.text()).not.toContain("Thinking");
    expect(nodes(io.tree()).some(node=>node._eventHandlers?.onClick)).toBe(false);
  }finally{io.close();}
});

test("completed history loads saved injection only after both round and context are opened",async()=>{
  const calls:string[]=[],snapshot={schema:1,trace_id:"turn",process_id:"a",first_seq:1,last_seq:1,event_count:1};
  const event={event:"provider_request_started",seq:1,time:"original",process_id:"a",model:"saved-model",detail_ref:"saved",
    body_ref:{schema:1,snapshot,first_seq:1,last_seq:1,event:"provider_request_started",process_id:"a",event_count:1,text_chars:0}};
  const client:any={callTool:async(name:string,args:any)=>{
    expect(name).toBe("runtime.trace");calls.push(args.action);
    if(args.action==="summary")return {trace_id:"turn",event_count:1};
    if(args.action==="index")return {snapshot,events:[event],next_seq:1,has_more:false};
    expect(args.action).toBe("detail");return {detail:inputView(args)};
  }};
  const msg:any={role:"assistant",content:"FINAL",timestamp:"original",status:"completed",trace_id:"turn",process_id:"a"};
  const draw=(verbose:boolean)=><MessageRow msg={msg} contentWidth={96} client={client} project="fixture-history" verbose={verbose}/>;
  const io=mount(draw(true));
  try{
    await pause(60);expect(calls).toEqual([]);
    io.click("Completed");await until(()=>io.text().includes("Injected context"));
    expect(calls).toEqual(["summary","index"]);expect(io.text()).not.toContain("Thinking");
    io.click("Injected context");await until(()=>io.text().includes("AGENTS.md"));
    io.click("AGENTS.md");await until(()=>io.text().includes("EXACT SOURCE TAIL"));
    expect(calls.filter(action=>action==="detail")).toHaveLength(2);
    io.rerender(draw(false));await pause(40);
    expect(io.text()).not.toContain("Injected context");expect(io.text()).not.toContain("EXACT SOURCE TAIL");
    expect(io.text()).not.toContain("Thinking");expect(io.text()).toContain("FINAL");
  }finally{io.close();}
});
