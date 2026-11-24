import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {VirtualConversation} from "./VirtualConversation.js";
import {ToolCallDisplay} from "./ToolCallDisplay.js";
import {TurnTimeline} from "./TurnTimeline.js";
import {BtwPanel} from "./BtwPanel.js";
import {InputProvider} from "../input/runtime.js";
import {traceBodyReader} from "../chat/traceSource.js";
import {colors} from "../theme/index.js";
import type {ChatMessage,StreamingRow,ToolCallCard} from "../types.js";

const delay=(ms=15)=>new Promise(resolve=>setTimeout(resolve,ms));
const text=(node:any):string=>node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
const nodes=(node:any):any[]=>[node,...(node.childNodes ?? []).flatMap(nodes)];
function mount(element:React.ReactNode){
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:110,rows:45});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const draw=(value:React.ReactNode)=><InputProvider><Box width={110} height={45} flexDirection="column">{value}</Box></InputProvider>;
  const root=renderSync(draw(element),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const tree=()=>(instances.get(stdout) as any).rootNode;
  return {root,tree,text:()=>text(tree()),rerender:(value:React.ReactNode)=>root.rerender(draw(value)),
    click:(label:string)=>{const node=nodes(tree()).reverse().find(n=>n._eventHandlers?.onClick && text(n).includes(label));expect(node).toBeDefined();node._eventHandlers.onClick({});},
    close:()=>{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
async function until(condition:()=>boolean){for(let i=0;i<180;i++){if(condition())return;await delay();}expect(condition()).toBe(true);}
const patch="--- a/config.py\n+++ b/config.py\n@@ -1,1 +1,1 @@\n-old = 1\n+UNIQUE_PATCH_VALUE = 2\n";
const tools:ToolCallCard[]=[
  {tool_name:"read_file",tool_call_id:"read",process_id:"A",target:"config.py",allowed:true,state:"completed",status_label:"OK",duration_ms:1,timestamp:"",result_text:"READ BODY"},
  {tool_name:"edit_file",tool_call_id:"edit",process_id:"A",target:"config.py",allowed:true,state:"completed",status_label:"OK",duration_ms:1,timestamp:"",result_text:"Saved.\n"+patch,diff:patch},
  {tool_name:"failed_tool",tool_call_id:"failed",process_id:"A",target:"",allowed:false,state:"error",status_label:"ERROR",duration_ms:1,timestamp:"",result_text:"Error: ERROR DETAIL ONCE",blocked_reason:"ERROR DETAIL ONCE"},
];
const user:ChatMessage={role:"user",content:"Inspect the folder",timestamp:"now",turn_id:"turn",message_id:"user:turn"};
const progress:ChatMessage={role:"assistant",kind:"progress",content:"PUBLIC STAGE",timestamp:"now",turn_id:"turn",process_id:"A",update_id:"u",message_id:"progress:u"};
const answer:ChatMessage={role:"assistant",content:"FINAL ANSWER",timestamp:"now",turn_id:"turn",process_id:"A",message_id:"outcome:turn",status:"completed",tools,
  notices:[{key:"n",kind:"completion_gate",label:"Completion blocked",detail:"GATE DETAILS",severity:"warning"}],
  activity:[{kind:"reasoning",text:"PRIVATE THINKING"},{kind:"progress",text:"PUBLIC STAGE",visibility:"public",process_id:"A",update_id:"u"},
    {kind:"tool",toolIndex:0},{kind:"tool",toolIndex:1},{kind:"tool",toolIndex:2},{kind:"notice",noticeIndex:0}]};
function conversation(messages:ChatMessage[],verbose=false,streaming:StreamingRow|null=null){
  return <VirtualConversation messages={messages} streaming={streaming} contentWidth={104} verbose={verbose} scrollRef={React.createRef<any>()}/>;
}

test("a terminal turn is user, one folded process, and one answer; polling and verbose never leak process rows",async()=>{
  const io=mount(conversation([user,progress,answer],true));
  try{
    await until(()=>io.text().includes("FINAL ANSWER"));
    expect(io.text()).toContain("Completed");
    for(const hidden of ["PUBLIC STAGE","PRIVATE THINKING","read_file","failed_tool","Completion blocked","UNIQUE_PATCH_VALUE"])expect(io.text()).not.toContain(hidden);
    io.click("Completed");await until(()=>io.text().includes("PUBLIC STAGE"));
    expect(io.text()).not.toMatch(/Thinking|PRIVATE THINKING/);
    expect(io.text().match(/PUBLIC STAGE/g)).toHaveLength(1);expect(io.text().match(/FINAL ANSWER/g)).toHaveLength(1);
    io.rerender(conversation([user,{...progress},{...answer}],true));await delay(60);expect(io.text()).toContain("PUBLIC STAGE");
    io.click("Completed");await until(()=>!io.text().includes("PUBLIC STAGE"));
    io.rerender(conversation([user,progress,{...answer}],false));await delay(60);expect(io.text()).not.toContain("read_file");
  }finally{io.close();}
});

test("successful names are gray without success text; only failed names turn red and every tool body really folds",async()=>{
  const io=mount(conversation([user,progress,answer]));
  try{
    await delay();io.click("Completed");await until(()=>io.text().includes("failed_tool"));
    expect(io.text()).not.toContain("READ BODY");expect(io.text()).not.toContain("ERROR DETAIL ONCE");expect(io.text()).not.toContain("UNIQUE_PATCH_VALUE");
    for(const [name,color] of [["read_file",colors.chat.reasoningFg],["edit_file",colors.chat.reasoningFg],["failed_tool",colors.danger]]){
      const node=nodes(io.tree()).find(n=>n.textStyles?.color===color && text(n)===name);expect(node).toBeDefined();
    }
    expect(nodes(io.tree()).some(n=>n.style?.backgroundColor===colors.dangerBg)).toBe(false);
    expect(io.text()).not.toMatch(/\bOK\b|成功|Success/);
    io.click("edit_file");await until(()=>io.text().includes("UNIQUE_PATCH_VALUE"));
    expect(io.text().match(/UNIQUE_PATCH_VALUE/g)).toHaveLength(1);
    io.click("edit_file");await until(()=>!io.text().includes("UNIQUE_PATCH_VALUE"));
    io.click("failed_tool");await until(()=>io.text().includes("ERROR DETAIL ONCE"));expect(io.text().match(/ERROR DETAIL ONCE/g)).toHaveLength(1);
    io.click("failed_tool");await until(()=>!io.text().includes("ERROR DETAIL ONCE"));
  }finally{io.close();}
});

test("the live container settles into a folded completed or failed record without leftover progress or tool rows",async()=>{
  const stream:StreamingRow={text:"",reasoning:"",timestamp:"2026-11-08T00:00:00Z",tools,notices:[],activity:answer.activity?.filter(x=>x.kind!=="notice")};
  const io=mount(conversation([user],false,stream));
  try{
    await until(()=>io.text().includes("PUBLIC STAGE"));expect(io.text()).toContain("Running");
    io.rerender(conversation([user,progress,answer]));await until(()=>io.text().includes("FINAL ANSWER"));
    expect(io.text()).not.toContain("PUBLIC STAGE");expect(io.text()).not.toContain("read_file");
    io.click("Completed");await until(()=>io.text().includes("read_file"));
    io.rerender(conversation([user,progress,{...answer}],true));await delay(50);expect(io.text()).toContain("PUBLIC STAGE");
    io.click("Completed");await until(()=>!io.text().includes("PUBLIC STAGE"));
    io.rerender(conversation([user,progress,{...answer,kind:"error",status:"failed",content:"[MAX_STEPS_EXHAUSTED]"}],true));await delay(50);
    expect(io.text()).toContain("Failed");expect(io.text().match(/MAX_STEPS_EXHAUSTED/g)).toHaveLength(1);expect(io.text()).not.toContain("failed_tool");
  }finally{io.close();}
});

test("an expanded saved tool loads its original arguments and full result once in the same body",async()=>{
  const snapshot={schema:1 as const,trace_id:"t",process_id:"A",first_seq:1,last_seq:1,event_count:1};
  const ref={schema:1 as const,snapshot,first_seq:1,last_seq:1,event:"tool_result",process_id:"A",event_count:1,text_chars:0};
  const calls:any[]=[];
  const client:any={callTool:async(_name:string,args:any)=>{calls.push(args);return args.action==="detail"
    ? {detail:{arguments:{path:"original.py"},formatted_result:"x".repeat(3000)+" EXACT FULL TAIL",data:{}}}
    : {snapshot,events:[{seq:1,time:"now",event:"tool_result",process_id:"A",tool_name:"read_file",tool_call_id:"read",result_preview:"PREVIEW",detail_ref:"trace-object:source"}],next_seq:1,has_more:false};}};
  const read=traceBodyReader(client,"fixture","t");
  const io=mount(<ToolCallDisplay tool={{...tools[0]!,trace_refs:[ref]}} readBody={read}/>);
  try{
    await delay();expect(calls).toHaveLength(0);io.click("read_file");await until(()=>io.text().includes("EXACT FULL TAIL"));
    expect(io.text()).toContain("original.py");expect(io.text()).not.toContain("PREVIEW");
    expect(calls.map(call=>call.action)).toEqual(["block","detail"]);
    io.click("read_file");await until(()=>!io.text().includes("EXACT FULL TAIL"));
    io.click("read_file");await until(()=>io.text().includes("EXACT FULL TAIL"));expect(calls).toHaveLength(2);
  }finally{io.close();}
});

test("a parallel success group remains expandable down to each actual call",async()=>{
  const group=tools.slice(0,2).map((tool,index)=>({...tool,tool_name:"web_fetch",tool_call_id:`web:${index}`,target:`source-${index}`,
    argument_preview:{url:`source-${index}`},result_text:`PAGE BODY ${index}`,diff:undefined,timestamp:new Date(1700000000000+index*10).toISOString(),duration_ms:100}));
  const io=mount(<TurnTimeline activity={group.map((_,toolIndex)=>({kind:"tool",toolIndex}))} tools={group} notices={[]} verbose={false} width={100}/>);
  try{
    await delay();expect(io.text()).toContain("Read 2 web pages");expect(io.text()).not.toContain("source-0");
    io.click("Read 2 web pages");await until(()=>io.text().includes("source-0"));expect(io.text()).not.toContain("PAGE BODY 0");
    io.click("source-0");await until(()=>io.text().includes("PAGE BODY 0"));
    io.click("Read 2 web pages");await until(()=>!io.text().includes("PAGE BODY 0"));
  }finally{io.close();}
});

test("answered user input stays outside the process while its full decision is available inside",async()=>{
  const decision:any={task_id:"turn",process_id:"A",decision_id:"q",kind:"clarification",question:"HISTORICAL QUESTION",why_user_must_decide:"A source choice",options:[],allow_free_form:true};
  const card:ChatMessage={role:"assistant",content:decision.question,timestamp:"now",turn_id:"turn",message_id:"decision:q",decision,decision_answer:"CHOSEN REPLY"};
  const outcome:ChatMessage={...answer,activity:[{kind:"notice",noticeIndex:0}],notices:[{key:"q",kind:"decision_required",decision_id:"q",label:"User decision required",detail:decision.question,severity:"warning"}]};
  const io=mount(conversation([user,card,outcome]));
  try{
    await delay();expect(io.text()).toContain("HISTORICAL QUESTION");expect(io.text()).toContain("CHOSEN REPLY");
    expect(io.text().match(/HISTORICAL QUESTION/g)).toHaveLength(1);
    io.click("Completed");await until(()=>nodes(io.tree()).some(node=>node._eventHandlers?.onClick && text(node).includes("HISTORICAL QUESTION")));
    expect(io.text()).not.toContain("User decision required");io.click("HISTORICAL QUESTION");await until(()=>io.text().includes("A source choice"));
    // Outer input, inner compact header, then the complete reading body.
    expect(io.text().match(/HISTORICAL QUESTION/g)).toHaveLength(3);expect(io.text().match(/CHOSEN REPLY/g)).toHaveLength(3);
    io.click("Completed");await until(()=>!io.text().includes("A source choice"));
    expect(io.text().match(/HISTORICAL QUESTION/g)).toHaveLength(1);expect(io.text().match(/CHOSEN REPLY/g)).toHaveLength(1);
  }finally{io.close();}
});

test("BTW uses the same terminal capsule instead of leaving verbose thinking and tool output outside",async()=>{
  let finish!:(value:any)=>void;const calls:string[]=[];
  const client:any={ready:true,close(){},callTool:async(name:string,_args:any,_timeout:number,onEvent:any)=>{
    calls.push(name);expect(name).toBe("runtime.btw");
    onEvent({event:"reasoning_delta",process_id:"A",delta:"BTW PRIVATE"});
    onEvent({event:"progress_summary",process_id:"A",update_id:"u",message:"BTW PUBLIC STAGE"});
    onEvent({event:"toolcall_start",process_id:"A",tool_call_id:"r",tool_name:"read_file"});
    onEvent({event:"tool_result",process_id:"A",tool_call_id:"r",tool_name:"read_file",result_preview:"BTW TOOL BODY",is_error:false});
    return new Promise(resolve=>{finish=resolve;});
  }};
  const io=mount(<InputProvider><BtwPanel client={client} project="p" processId="A" question="Side question"
    answer="" sidecarId="s" processes={{}} cols={110} maxBodyRows={15} verbose onDismiss={()=>{}} onApply={()=>{}}/></InputProvider>);
  try{
    await until(()=>Boolean(finish));finish({answer:"BTW FINAL",sidecar_id:"s"});await until(()=>io.text().includes("BTW FINAL"));
    expect(io.text()).toContain("Completed");
    for(const hidden of ["BTW PRIVATE","BTW PUBLIC STAGE","read_file","BTW TOOL BODY"])expect(io.text()).not.toContain(hidden);
    io.click("Completed");await until(()=>io.text().includes("read_file"));
    expect(io.text()).not.toContain("BTW TOOL BODY");
    io.click("read_file");await until(()=>io.text().includes("BTW TOOL BODY"));
    expect(io.text().match(/BTW FINAL/g)).toHaveLength(1);expect(io.text().match(/BTW PUBLIC STAGE/g)).toHaveLength(1);
    io.click("Completed");await until(()=>!io.text().includes("BTW TOOL BODY"));expect(calls).toEqual(["runtime.btw"]);
  }finally{io.close();}
});
