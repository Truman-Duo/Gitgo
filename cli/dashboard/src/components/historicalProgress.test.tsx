import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {MessageRow} from "./MessageRow.js";
import {VirtualConversation} from "./VirtualConversation.js";
import type {ChatMessage} from "../types.js";
import type {TraceRecord} from "../traceView.js";

const delay = (ms = 10) => new Promise(resolve => setTimeout(resolve, ms));
const nodeText = (node: any): string => node.nodeValue ?? (node.childNodes ?? []).map(nodeText).join("");
const nodes = (node:any): any[] => [node,...(node.childNodes ?? []).flatMap(nodes)];
function mount(element: React.ReactNode) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY:false,columns:100,rows:50}); stdout.on("data",()=>{});
  const stderr: any = new PassThrough(); stderr.on("data",()=>{});
  const draw = (value: React.ReactNode) => <Box width={100} height={50} flexDirection="column">{value}</Box>;
  const root = renderSync(draw(element), {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  return {root, text:() => nodeText((instances.get(stdout) as any).rootNode),tree:() => (instances.get(stdout) as any).rootNode,
    rerender(value:React.ReactNode){root.rerender(draw(value));},
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
async function until(condition: () => boolean) {
  for (let attempt = 0; attempt < 200; attempt++) {if (condition()) return; await delay();}
  expect(condition()).toBe(true);
}
const message: ChatMessage = {role:"assistant",content:"FINAL ANSWER",timestamp:"2026-11-01T12:00:00Z",
  status:"completed",trace_id:"old-turn",process_id:"A",turn_id:"old-turn",message_id:"outcome:old-turn"};
const publicStage = "Historical public stage " + "长".repeat(600) + " EXACT PUBLIC TAIL";
const source = [
  {event:"provider_request_started",task_kind:"answer"},
  {event:"progress_summary",message:publicStage},
  {event:"reasoning_delta",delta:"PRIVATE REASONING"},
  {event:"text_delta",visibility:"public",delta:"Intermediate public "},
  {event:"text_delta",visibility:"public",delta:"explanation"},
  {event:"toolcall_start",tool_name:"read_file",tool_call_id:"ok"},
  {event:"tool_result",tool_name:"read_file",tool_call_id:"ok",result_preview:"PRIVATE SUCCESS BODY"},
  {event:"text_delta",visibility:"verbose",delta:"PRIVATE DRAFT"},
  {event:"toolcall_start",tool_name:"write_file",tool_call_id:"failure"},
  {event:"tool_result",tool_name:"write_file",tool_call_id:"failure",is_error:true,error:"Recorded access failure"},
  {event:"text_delta",visibility:"public",delta:"FINAL TRACE DRAFT"},
  {event:"agent_complete",status:"completed"},
  {event:"progress_summary",process_id:"B",message:"Child public stage"},
  {event:"provider_usage",usage:{input_tokens:7,output_tokens:5}},
].map((event,index) => ({process_id:"A",seq:index+1,time:"2026-11-01T12:00:00Z",...event})) as TraceRecord[];

/** Replay original records through the same summary/index/block contract. */
function fixture(events = source) {
  const calls: any[] = [];
  const client: any = {callTool: async (name:string, args:any) => {
    expect(name).toBe("runtime.trace"); calls.push(args);
    const selected = events.filter(event => !args.process_id || event.process_id === args.process_id);
    const snapshot = {schema:1,trace_id:args.trace_id,process_id:args.process_id || "",
      first_seq:selected[0]?.seq || 0,last_seq:selected.at(-1)?.seq || 0,event_count:selected.length};
    if (args.action === "summary") return {trace_id:args.trace_id,event_count:selected.length};
    if (args.action === "block") {
      const ref = args.body_ref;
      const body = events.filter(event => event.seq >= ref.first_seq && event.seq <= ref.last_seq && event.process_id === ref.process_id);
      return {snapshot:ref.snapshot,events:body,next_seq:ref.last_seq,has_more:false};
    }
    expect(args.action).toBe("index");
    return {snapshot,events:selected.map(event => {
      const {delta,result_preview,...header} = event;
      return {...header,message:typeof event.message === "string" ? event.message.slice(0,512) : undefined,
        body_ref:{schema:1,snapshot,first_seq:event.seq,last_seq:event.seq,event:event.event,
          process_id:event.process_id,event_count:1,text_chars:String(delta || "").length}};
    }),next_seq:snapshot.last_seq || -1,has_more:false};
  }};
  return {client,calls};
}

test("an old folded turn hides all process records and expands exact public bodies on demand", async () => {
  const {client,calls} = fixture();
  const draw = (expanded = false, includeDescendants = false) => <MessageRow msg={message} client={client}
    project="history" contentWidth={94} expanded={expanded} includeDescendants={includeDescendants}/>;
  let io = mount(draw());
  try {
    await delay(35); expect(calls).toHaveLength(0); expect(io.text()).not.toContain("Historical public stage");
    io.rerender(draw(true));
    await until(() => io.text().includes("EXACT PUBLIC TAIL") && io.text().includes("Intermediate public explanation"));
    expect(io.text()).toContain("write_file");
    expect(io.text()).not.toContain("Recorded access failure");
    for (const hidden of ["PRIVATE REASONING","PRIVATE DRAFT","PRIVATE SUCCESS BODY","FINAL TRACE DRAFT","Child public stage"]) {
      expect(io.text()).not.toContain(hidden);
    }
    const readSequences = calls.filter(call=>call.action==="block").map(call=>call.body_ref.first_seq);
    for (const hiddenSequence of [3,6,7,8,11,13]) expect(readSequences).not.toContain(hiddenSequence);
    expect(calls.slice(0,2).map(call=>call.action)).toEqual(["summary","index"]);
    const tool=nodes(io.tree()).find(node=>node._eventHandlers?.onClick && nodeText(node).includes("read_file"));
    expect(tool).toBeDefined();tool._eventHandlers.onClick({}); await until(() => io.text().includes("PRIVATE SUCCESS BODY"));
    expect(io.text().match(/Historical public stage/g)).toHaveLength(1);
    expect(io.text().match(/Intermediate public explanation/g)).toHaveLength(1);
    expect(io.text()).not.toContain("PRIVATE REASONING");
    expect(io.text()).not.toContain("PRIVATE DRAFT");
    io.rerender(draw()); await until(() => !io.text().includes("PRIVATE SUCCESS BODY") && !io.text().includes("EXACT PUBLIC TAIL"));
    expect(calls.filter(call=>call.action==="index")).toHaveLength(1);
    io.close(); io = mount(draw());
    await delay(30);expect(io.text()).not.toContain("EXACT PUBLIC TAIL");io.rerender(draw(true));
    await until(() => io.text().includes("EXACT PUBLIC TAIL"));
    expect(calls.filter(call=>call.action==="index")).toHaveLength(1);
    io.rerender(draw(true,true)); await until(() => io.text().includes("Child public stage"));
    expect(io.text()).toContain("Subprocess");
  } finally {io.close();}
});

test("the shared conversation viewport deduplicates new saved updates while retaining legacy updates", async () => {
  const events = [...source.slice(0,12),{seq:13,time:"now",process_id:"A",event:"progress_summary",update_id:"u",message:"Modern saved update"}] as TraceRecord[];
  const {client} = fixture(events);
  const messages: ChatMessage[] = [
    {role:"user",content:"Prior request",timestamp:"now",turn_id:"old-turn"},
    {role:"assistant",kind:"progress",content:"Modern saved update",timestamp:"now",turn_id:"old-turn",process_id:"A",update_id:"u"},
    message,
  ];
  const scrollRef = React.createRef<any>();
  const io = mount(<VirtualConversation messages={messages} streaming={null} contentWidth={94} scrollRef={scrollRef} client={client} project="history"/>);
  try {
    await delay(30);expect(io.text()).not.toContain("Modern saved update");
    const round=nodes(io.tree()).find(node=>node._eventHandlers?.onClick && /Completed|已完成/.test(nodeText(node)));
    expect(round).toBeDefined();round._eventHandlers.onClick({});
    await until(() => io.text().includes("EXACT PUBLIC TAIL"));
    expect(io.text().match(/Modern saved update/g)).toHaveLength(1);
    expect(io.text().match(/FINAL ANSWER/g)).toHaveLength(1);
  } finally {io.close();}
});

test("retained inline legacy progress stays inside its capsule without requiring an update id or trace", async () => {
  const io = mount(<MessageRow msg={{...message,trace_id:undefined,activity:[
    {kind:"progress",text:"Old inline public update",visibility:"public"},
    {kind:"progress",text:"PRIVATE INLINE UPDATE",visibility:"verbose"},
  ]}} contentWidth={94} expanded/>);
  try {
    await until(() => io.text().includes("Old inline public update"));
    expect(io.text()).not.toContain("PRIVATE INLINE UPDATE");
  } finally {io.close();}
});

test("leaving a collapsed historical turn cancels later index and body reads", async () => {
  let finish!: (result:any) => void;
  const calls: any[] = [];
  const client: any = {callTool: async (_name:string,args:any) => {
    calls.push(args);
    if (args.action === "summary") return {trace_id:"old-turn",event_count:2};
    return new Promise(resolve => {finish=resolve;});
  }};
  const io = mount(<MessageRow msg={message} client={client} project="history" contentWidth={94} expanded/>);
  try {
    await until(() => Boolean(finish)); io.root.unmount();
    finish({events:[],next_seq:0,has_more:true}); await delay(30);
    expect(calls.map(call=>call.action)).toEqual(["summary","index"]);
  } finally {io.close();}
});

test("an unavailable index keeps the final answer and retries through the existing round disclosure", async () => {
  const replay = fixture(); let attempts = 0;
  const client: any = {callTool: async (name:string,args:any) => {
    if (args.action === "index" && attempts++ === 0) throw new Error("INDEX READ FAILED");
    return replay.client.callTool(name,args);
  }};
  const io = mount(<MessageRow msg={message} client={client} project="retry" contentWidth={94}/>);
  try {
    await delay(25);
    let header = nodes(io.tree()).find(node=>node._eventHandlers?.onClick && /Completed|已完成/.test(nodeText(node)));
    header._eventHandlers.onClick({});
    await until(() => io.text().includes("INDEX READ FAILED"));
    expect(io.text()).toContain("FINAL ANSWER");
    header = nodes(io.tree()).find(node=>node._eventHandlers?.onClick && /Completed|已完成/.test(nodeText(node)));
    expect(header).toBeDefined(); header._eventHandlers.onClick({});await delay(180);
    header = nodes(io.tree()).find(node=>node._eventHandlers?.onClick && /Completed|已完成/.test(nodeText(node)));
    header._eventHandlers.onClick({});
    await until(() => io.text().includes("EXACT PUBLIC TAIL"));
    expect(attempts).toBe(2);
    expect(io.text()).not.toContain("INDEX READ FAILED");
  } finally {io.close();}
});

test("a late historical response cannot write into another project's conversation", async () => {
  const replay = fixture([{seq:1,time:"now",event:"progress_summary",process_id:"A",message:"New project public stage"}]);
  let finish!: (result:any) => void;
  const calls: any[] = [];
  const client: any = {callTool: async (name:string,args:any) => {
    calls.push(args);
    if (args.project !== "old-project") return replay.client.callTool(name,args);
    if (args.action === "summary") return {trace_id:"old-turn",event_count:1};
    return new Promise(resolve => {finish=resolve;});
  }};
  const draw = (project:string) => <MessageRow msg={message} client={client} project={project} contentWidth={94} expanded/>;
  const io = mount(draw("old-project"));
  try {
    await until(() => Boolean(finish)); io.rerender(draw("new-project"));
    await until(() => io.text().includes("New project public stage"));
    finish({events:[{seq:1,event:"progress_summary",message:"STALE PROJECT CONTENT"}],next_seq:1,has_more:true});
    await delay(40);
    expect(io.text()).not.toContain("STALE PROJECT CONTENT");
    expect(calls.filter(call=>call.project==="old-project").map(call=>call.action)).toEqual(["summary","index"]);
  } finally {io.close();}
});

test("expired trace storage preserves retained public facts and does not invent missing progress", async () => {
  const {client} = fixture([]);
  const io = mount(<MessageRow msg={{...message,activity:[{kind:"progress",visibility:"public",text:"Retained historical update"}]}}
    client={client} project="expired" contentWidth={94} expanded/>);
  try {
    await until(() => io.text().includes("Retained historical update")); await delay(40);
    expect(io.text()).toContain("Retained historical update");
    io.rerender(<MessageRow msg={message} client={client} project="empty" contentWidth={94} expanded/>); await delay(40);
    expect(io.text()).toContain("FINAL ANSWER");
    expect(io.text()).not.toContain("Retained historical update");
    expect(io.text()).not.toContain("Historical public stage");
  } finally {io.close();}
});
