import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import stripAnsi from "strip-ansi";
import { Box, renderSync } from "@anthropic/ink";
import { MessageRow } from "./MessageRow.js";
import type { BackendClient } from "../backend/client.js";
import {projectConversationRounds} from "../chat/conversationRounds.js";
import type {ChatMessage} from "../types.js";
import instances from "../../vendor/ink/src/core/instances.js";

function nodeText(node: any): string {
  return node.nodeValue || node.childNodes?.map(nodeText).join("") || "";
}

test("public progress belongs to the completed capsule and appears once on expansion", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString(); });
  const messages = projectConversationRounds([{role:"assistant", content:"Final answer", timestamp:"now",
    turn_id:"t", status:"completed", activity:[
      {kind:"progress", text:"Public stage update", visibility:"public", process_id:"a", update_id:"u"},
      {kind:"progress", text:"Private internal draft", visibility:"verbose", process_id:"a"},
    ]}], null);
  const draw = (expanded: boolean) => <Box flexDirection="column" width={100}>
    {messages.map((msg, index) => <MessageRow key={index} msg={msg as ChatMessage} contentWidth={96} expanded={expanded}/>)}
  </Box>;
  const root = renderSync(draw(false), {stdin, stdout, stderr, patchConsole:false, exitOnCtrlC:false});
  try {
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(stripAnsi(output)).not.toContain("Public stage update");
    expect(stripAnsi(output)).not.toContain("Private internal draft");
    output = "";
    root.rerender(draw(true));
    await new Promise(resolve => setTimeout(resolve, 50));
    const tree = nodeText((instances.get(stdout) as any).rootNode);
    expect(tree.match(/Public stage update/g)).toHaveLength(1);
    expect(tree).not.toContain("Private internal draft");
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
});

test("failed round keeps its error and reads cleanup evidence through the existing trace client", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr = new PassThrough();
  let output = "";
  const calls: any[] = [];
  const snapshot = {schema:1,trace_id:"trace",process_id:"p",first_seq:1,last_seq:2,event_count:2};
  const events = [
    {seq:1,time:"now",event:"tool_result",process_id:"p",tool_name:"read_file",tool_call_id:"read",is_error:true,error:"cancelled",duration_ms:10},
    {seq:2,time:"now",event:"progress_summary",phase:"tool_notice",process_id:"p",code:"PROCESS_CLEANUP_INCOMPLETE",
      message:"Cleanup remains unknown; retain resource ownership."},
  ];
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString(); });
  const client = {callTool: async (name: string, args: any) => {
    calls.push({name, args});
    if(args.action==="summary") return {trace_id:"trace",event_count:2,event_counts:{tool_result:1,progress_summary:1}};
    if(args.action==="index") return {snapshot,events:events.map(({message,...event})=>({...event,
      body_ref:{schema:1,snapshot,first_seq:event.seq,last_seq:event.seq,event:event.event,process_id:"p",event_count:1,text_chars:0}})),
      next_seq:2,has_more:false};
    return {snapshot,events:[events[args.body_ref.first_seq-1]],next_seq:args.body_ref.last_seq,has_more:false};
  }} as unknown as BackendClient;
  const root = renderSync(<Box width={100}><MessageRow msg={{
    role: "assistant", content: "[Error: cancelled]", timestamp: "now", kind: "error",
    status: "cancelled", trace_id: "trace", process_id: "p",
  }} contentWidth={96} project="fixture" client={client} expanded/></Box>,
  {stdin, stdout, stderr: stderr as unknown as NodeJS.WriteStream, patchConsole: false, exitOnCtrlC: false});
  try {
    for(let attempt=0;attempt<100&&!stripAnsi(output).includes("Cleanup remains unknown");attempt++)await new Promise(resolve=>setTimeout(resolve,20));
    const plain = stripAnsi(output);
    expect(plain).toContain("Failed");
    expect(plain).toContain("[Error: cancelled]");
    expect(plain).toContain("read_file");
    expect(plain).toContain("Cleanup remains unknown");
    expect(plain).not.toContain("Completed");
    expect(calls.map(call=>call.args.action)).toEqual(["summary","index","block"]);
    expect(calls[0].name).toBe("runtime.trace");
    expect(calls[0].args.trace_id).toBe("trace");
    expect(calls[0].args.process_id).toBe("p");
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});

test("leaving a historical round stops pagination when an in-flight page returns", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 40;
  const stderr: any = new PassThrough(); stdout.on("data", () => {});
  const calls: any[] = [];
  let complete!: (value: any) => void;
  const client = {callTool: async (_name: string, args: any) => {
    calls.push(args);
    if(args.action==="summary") return {trace_id:"trace-cancel-page",event_count:3};
    return await new Promise(resolve => {complete=resolve;});
  }} as unknown as BackendClient;
  const root = renderSync(<Box width={100}><MessageRow msg={{role:"assistant", content:"Answer", timestamp:"now",
    status:"completed", trace_id:"trace-cancel-page", process_id:"a"}} contentWidth={96}
    client={client} project="page-cancel" expanded/></Box>,
    {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(calls.map(call=>call.action)).toEqual(["summary","index"]);
    root.unmount();
    complete({events:[{seq:1,event:"reasoning_delta",process_id:"a",delta:"late private page"}],next_seq:1,has_more:true});
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(calls).toHaveLength(2);
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
});
