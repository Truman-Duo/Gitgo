import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {MessageRow} from "./MessageRow.js";
import type {ChatMessage} from "../types.js";
import type {TraceSnapshot, TraceBodyRef} from "../chat/traceSource.js";
const wait = (ms = 80) => new Promise(resolve => setTimeout(resolve, ms));
const text = (node: any): string => node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
function mount(element: React.ReactNode) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY:false,columns:90,rows:30}); stdout.on("data",()=>{});
  const stderr: any = new PassThrough(); stderr.on("data",()=>{});
  const root = renderSync(<Box width={90} flexDirection="column">{element}</Box>, {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  return {root, tree:()=>(instances.get(stdout) as any).rootNode,
    rerender(value:React.ReactNode){root.rerender(<Box width={90} flexDirection="column">{value}</Box>);},
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
const snapshot: TraceSnapshot = {schema:1,trace_id:"turn",process_id:"A",first_seq:1,last_seq:60001,event_count:60001};
const ref: TraceBodyRef = {schema:1,snapshot,first_seq:1,last_seq:60000,event:"text_delta",process_id:"A",event_count:60000,text_chars:60000};
const message: ChatMessage = {role:"assistant",content:"FINAL",timestamp:"now",status:"completed",trace_id:"turn",process_id:"A"};
test("opening a round reads public stage text beyond 50000 events without another disclosure", async () => {
  const calls: any[] = [];
  const client: any = {callTool: async (_name: string, args: any) => {
    calls.push(args);
    if (args.action === "summary") return {trace_id:"turn",event_count:60001,event_counts:{text_delta:60000}};
    if (args.action === "index") return {snapshot,events:[
      {seq:1,event:"text_delta",visibility:"public",process_id:"A",body_ref:ref},
      {seq:60001,event:"provider_request_started",process_id:"A",task_kind:"answer",
        body_ref:{...ref,first_seq:60001,last_seq:60001,event:"provider_request_started",event_count:1,text_chars:0}},
    ],next_seq:60001,has_more:false};
    const start = args.after_seq + 1, end = Math.min(60000, start + 499);
    return {snapshot,events:Array.from({length:end-start+1},(_,index)=>({seq:start+index,time:"now",event:"text_delta",visibility:"public",process_id:"A",
      delta:start+index===60000?"EXACT END e\u0301 中文":"x"})),next_seq:end,has_more:end<60000};
  }};
  const draw = (expanded = true) => <MessageRow msg={message} contentWidth={86} client={client} project="fixture" verbose={false} expanded={expanded}/>;
  const io = mount(draw(false));
  try {
    await wait(100);expect(calls).toHaveLength(0);
    io.rerender(draw());
    for(let attempt=0;attempt<30 && !text(io.tree()).includes("EXACT END");attempt++)await wait(50);
    expect(calls.slice(0,2).map(item=>item.action)).toEqual(["summary","index"]);
    expect(text(io.tree())).not.toMatch(/Thinking|思考|Progress|阶段说明/);
    expect(text(io.tree())).toContain("EXACT END e\u0301 中文");
    expect(calls.filter(item=>item.action==="block" && item.body_ref.event==="text_delta").length).toBe(120);
    io.rerender(draw(false)); await wait(250);
    expect(text(io.tree())).not.toContain("EXACT END");
  } finally {io.close();}
}, 10000);
