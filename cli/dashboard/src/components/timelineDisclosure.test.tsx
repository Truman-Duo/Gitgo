import React from "react";
import {expect, test} from "bun:test";
import chalk from "chalk";
import {PassThrough} from "node:stream";
import {Box, renderSync, type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {TurnTimeline} from "./TurnTimeline.js";
import {MessageRow} from "./MessageRow.js";
import {StreamingMessage} from "./StreamingMessage.js";
import {ToolCallDisplay} from "./ToolCallDisplay.js";
import {VirtualConversation} from "./VirtualConversation.js";
import {RuntimeNoticeRow} from "./RuntimeNoticeList.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {initStreamState,reduceStreamEvent} from "../daemon/streamReducer.js";
import type {TraceBodyReader} from "../chat/traceSource.js";
import type {ChatMessage} from "../types.js";
const wait = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const text = (node: any): string => node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
const nodes = (node: any): any[] => [node, ...(node.childNodes ?? []).flatMap(nodes)];
function mount(element: React.ReactNode,columns=90) {
  const stdin: any = new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough();Object.assign(stdout,{isTTY:false,columns,rows:30});stdout.on("data",()=>{});
  const stderr: any = new PassThrough();stderr.on("data",()=>{});
  const draw=(value:React.ReactNode)=><Box width={columns} height={30} flexDirection="column">{value}</Box>;
  const root=renderSync(draw(element),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  return {root, tree:()=>(instances.get(stdout) as any).rootNode,
    styles(x:number,y:number){const ink=instances.get(stdout) as any;
      return ink.stylePool.get(cellAt(ink.frontFrame.screen,x,y)!.styleId).map((style:any)=>style.code) as string[];},
    rerender(value:React.ReactNode){root.rerender(draw(value));},
    frame(){const screen=(instances.get(stdout) as any).frontFrame.screen;
      return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char??" ").join(""));},
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}

test("active tool uses one clickable directional marker and retains expansion when finished",async()=>{
  const previousColorLevel=chalk.level;
  chalk.level=3;
  const tool={tool_name:"read_file",tool_call_id:"stable",target:"file.txt",timestamp:new Date().toISOString(),
    state:"running" as const,allowed:true,status_label:"",duration_ms:0,result_text:"TRACKED FILE DETAIL"};
  const io=mount(<ToolCallDisplay tool={tool} width={80}/>);
  const line=()=>io.frame().find(row=>row.includes("read_file"))!.trimStart();
  const markerColor=()=>{const y=io.frame().findIndex(row=>row.includes("read_file"));
    return io.styles(io.frame()[y]!.search(/[▸▾]/),y).find(style=>style.startsWith("\x1b[38;2;"));};
  try{
    await wait();
    const colors=new Set<string|undefined>();
    for(let i=0;i<7&&colors.size<2;i++){
      expect(line()).toMatch(/^▸ read_file/);colors.add(markerColor());await wait(90);
    }
    expect(colors).toEqual(new Set(["\x1b[38;2;210;153;34m","\x1b[38;2;158;118;36m"]));
    const header=nodes(io.tree()).find(node=>node._eventHandlers?.onClick&&text(node).includes("read_file"));
    expect(header).toBeDefined();header._eventHandlers.onClick({});
    await wait(240);expect(line()).toMatch(/^▾ read_file/);
    io.rerender(<ToolCallDisplay tool={{...tool,state:"completed",duration_ms:200}} width={80}/>);
    await wait(240);expect(line()).toMatch(/^▾ read_file/);expect(colors.has(markerColor())).toBe(false);
    expect(text(io.tree())).toContain("TRACKED FILE DETAIL");
  } finally {io.close();chalk.level=previousColorLevel;}
});

test("manually expanded live round stays open when the durable answer arrives",async()=>{
  const scrollRef=React.createRef<ScrollBoxHandle>();
  const timestamp=new Date().toISOString();
  const activity=[{kind:"progress" as const,text:"VERIFIED PHASE",visibility:"public" as const}];
  const streaming={timestamp,activity,text:"",reasoning:"",tools:[],notices:[]};
  const user:ChatMessage={role:"user",content:"Start",timestamp,message_id:"u",turn_id:"t"};
  const answer:ChatMessage={role:"assistant",content:"FINAL",timestamp,message_id:"a",turn_id:"t",status:"completed"};
  const draw=(live:boolean)=><VirtualConversation messages={live ? [user] : [user,answer]}
    streaming={live ? streaming : null} contentWidth={86} scrollRef={scrollRef}/>;
  const io=mount(draw(true));
  const click=()=>{
    const header=nodes(io.tree()).find(node=>node._eventHandlers?.onClick&&text(node).includes("Running"));
    expect(header).toBeDefined();header._eventHandlers.onClick({});
  };
  try{
    await wait();expect(text(io.tree())).toContain("VERIFIED PHASE");
    click();await wait();expect(text(io.tree())).not.toContain("VERIFIED PHASE");
    click();await wait();expect(text(io.tree())).toContain("VERIFIED PHASE");
    io.rerender(draw(false));await wait();
    expect(io.frame().find(row=>row.includes("Completed"))).toContain("▾ Completed");
    expect(text(io.tree())).toContain("VERIFIED PHASE");
    expect(text(io.tree())).toContain("FINAL");
  } finally {io.close();}
});
test("current thinking belongs beneath its public report and never returns in completed history",async()=>{
  let state=reduceStreamEvent(initStreamState(),{event:"reasoning_delta",process_id:"a",delta:"FIRST PRIVATE SEGMENT"});
  const draw=()=><StreamingMessage streaming={{...state,timestamp:new Date().toISOString()}} verbose contentWidth={86}/>;
  const io=mount(draw());
  try{
    await wait();expect(text(io.tree())).toContain("Thinking");expect(text(io.tree())).toContain("FIRST PRIVATE SEGMENT");
    state=reduceStreamEvent(state,{event:"progress_summary",process_id:"a",update_id:"u",message:"PUBLIC UPDATE"});
    io.rerender(draw());await wait();
    expect(text(io.tree())).toContain("PUBLIC UPDATE");expect(text(io.tree())).toContain('FIRST PRIVATE SEGMENT');
    expect(text(io.tree())).not.toMatch(/Thinking|Progress/);
    expect(nodes(io.tree()).some(node=>node._eventHandlers?.onClick&&text(node).includes("PUBLIC UPDATE"))).toBe(false);
    state=reduceStreamEvent(state,{event:"provider_request_started",process_id:"a"});
    io.rerender(draw());await wait();expect(text(io.tree())).toContain("Thinking");expect(text(io.tree())).not.toContain("PRIVATE SEGMENT");
    state=reduceStreamEvent(state,{event:"reasoning_delta",process_id:"a",delta:"SECOND PRIVATE SEGMENT"});
    io.rerender(draw());await wait();
    expect(text(io.tree())).toContain("SECOND PRIVATE SEGMENT");expect(text(io.tree())).not.toContain("FIRST PRIVATE SEGMENT");
    state=reduceStreamEvent(state,{event:"toolcall_start",process_id:"a",tool_name:"read_file",tool_call_id:"r"});
    io.rerender(draw());await wait();expect(text(io.tree())).toContain("read_file");expect(text(io.tree())).not.toMatch(/Thinking|PRIVATE SEGMENT/);
    state=reduceStreamEvent(state,{event:"agent_complete",process_id:"a",status:"completed"});
    io.rerender(<MessageRow msg={{role:"assistant",content:"FINAL ANSWER",timestamp:"now",status:"completed",
      reasoning:state.reasoning,activity:state.activity,tools:state.tools,notices:state.notices,
      provider_usage:{input_tokens:123,output_tokens:12}}} expanded verbose contentWidth={86}/>);
    await wait();expect(text(io.tree())).toContain("PUBLIC UPDATE");
    expect(text(io.tree())).not.toMatch(/Thinking|PRIVATE SEGMENT|Progress|123 in/);
  }finally{io.close();}
});

for(const language of ["en","zh"] as const)for(const columns of [90,34]){
  test(`nested process, tool group, tool and result use successive cell indents (${language}, ${columns})`,async()=>{
    const tools=[0,1].map(index=>({tool_name:"web_fetch",tool_call_id:"web"+index,target:"page"+index,
      state:"completed" as const,allowed:true,status_label:"OK",result_text:"RESULT "+index,duration_ms:100,
      timestamp:new Date(1700000000000+index*10).toISOString()}));
    const msg:ChatMessage={role:"assistant",content:"FINAL",timestamp:"now",status:"completed",tools,
      notices:[{key:"end",kind:"agent_complete",label:"Main process ended",detail:"unknown",severity:"info"}],
      activity:[{kind:"progress",text:"已读取目录",visibility:"public"},{kind:"tool",toolIndex:0},{kind:"tool",toolIndex:1},{kind:"notice",noticeIndex:0}]};
    const io=mount(<LanguageContext.Provider value={language}><MessageRow msg={msg} contentWidth={columns-2} expanded/></LanguageContext.Provider>,columns);
    const click=(label:string)=>{const node=nodes(io.tree()).find(n=>n._eventHandlers?.onClick&&text(n).includes(label));expect(node).toBeDefined();node._eventHandlers.onClick({});};
    const indent=(needle:string)=>{const line=io.frame().find(line=>line.includes(needle));expect(line).toBeDefined();return line!.search(/\S/);};
    try{
      await wait();
      const completed=language==="en"?"Completed":"已完成",group=language==="en"?"Read 2 web pages":"读取 2 个网页";
      expect(indent(completed)).toBe(2);expect(indent(group)).toBe(4);
      expect(indent("已读取目录")).toBe(4);
      expect(text(io.tree())).not.toMatch(/阶段说明|Progress|◆/);
      expect(nodes(io.tree()).some(n=>n._eventHandlers?.onClick&&text(n).includes("Main process ended"))).toBe(false);
      click(group);await wait(250);expect(indent("web_fetch")).toBe(6);
      click("page0");await wait(250);expect(indent("RESULT 0")).toBe(8);
      click(group);await wait(250);expect(text(io.tree())).not.toContain("RESULT 0");
    }finally{io.close();}
  });
}

test("search groups count requests without implying a count of unique websites",async()=>{
  const tools=[0,1].map(index=>({tool_name:"web_search",tool_call_id:"search"+index,target:"query"+index,
    state:"completed" as const,allowed:true,status_label:"OK",duration_ms:100,
    timestamp:new Date(1700000000000+index*10).toISOString()}));
  const io=mount(<TurnTimeline activity={tools.map((_,toolIndex)=>({kind:"tool",toolIndex}))} tools={tools} notices={[]} verbose={false} width={86}/>);
  try{await wait();expect(text(io.tree())).toContain("Search · 2 requests");expect(text(io.tree())).not.toMatch(/\(2\)|2 web/);}
  finally{io.close();}
});

test("a plain terminal failure keeps its full saved error without a second disclosure",async()=>{
  const full="Saved failure "+"长".repeat(200)+" EXACT ERROR TAIL";
  let reads=0;
  const read=(async()=>[]) as TraceBodyReader;
  read.detail=async()=>{reads++;return {legacy_record:{event:"agent_complete",actor_kind:"supervisor",
    outcome:{status:"failed",error:{message:full}}}};};
  const io=mount(<RuntimeNoticeRow notice={{key:"failed",kind:"agent_complete",label:"Main process failed",
    severity:"error",detail:"Saved failure…",detail_ref:"saved"}} verbose={false} readBody={read}/>);
  try{
    await wait();expect(text(io.tree())).toContain(full);expect(reads).toBe(1);
    expect(nodes(io.tree()).some(node=>node._eventHandlers?.onClick)).toBe(false);
    expect(text(io.tree())).not.toContain("◆");
  }finally{io.close();}
});
test("a folded completed turn hides failures and performs no trace reads",async()=>{
  const reads:string[]=[];
  const tools=[{tool_name:"read_file",target:"good.txt",allowed:true,status_label:"OK",duration_ms:1,timestamp:"",state:"completed" as const},
    {tool_name:"web_fetch",target:"failed-url",allowed:false,status_label:"ERROR",duration_ms:1,timestamp:"",state:"error" as const,blocked_reason:"RECORDED TOOL FAILURE"}];
  const message:ChatMessage={role:"assistant",content:"Final answer",timestamp:"now",trace_id:"saved",status:"completed",tools,
    activity:[{kind:"reasoning",text:"PRIVATE REASONING"},{kind:"tool",toolIndex:0},{kind:"tool",toolIndex:1}]};
  const io=mount(<MessageRow msg={message} contentWidth={86} expanded={false} client={{callTool:async(name:string,args:any)=>{
    expect(name).toBe("runtime.trace");reads.push(args.action);
    if(args.action==="summary")return {trace_id:args.trace_id,event_count:0};
    expect(args.action).toBe("index");return {snapshot:{schema:1,trace_id:args.trace_id,process_id:args.process_id||"",first_seq:0,last_seq:0,event_count:0},events:[],next_seq:-1,has_more:false};
  }} as any} project="fixture"/>);
  try{await wait();expect(text(io.tree())).not.toContain("RECORDED TOOL FAILURE");expect(text(io.tree())).not.toContain("good.txt");
    expect(text(io.tree())).not.toContain("PRIVATE REASONING");expect(reads).toEqual([]);
  }finally{io.close();}
});
