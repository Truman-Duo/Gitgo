import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {InputProvider,InputLayer} from "../input/runtime.js";
import type {ChatScrollHandle} from "../types.js";
import {ChatPanel} from "./ChatPanel.js";
import {AgentDetail} from "./AgentDetail.js";
import {BtwPanel} from "./BtwPanel.js";
const wait=()=>new Promise(r=>setTimeout(r,80));
for(const subprocess of [false,true])test("BTW keeps keys within its "+(subprocess?"B":"A")+" discussion until parent focus is selected",async()=>{
 const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=100;stdout.rows=30;stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const client:any={ready:true,close(){},async callTool(){return {answer:("answer line"+String.fromCharCode(10,10)).repeat(35),sidecar_id:"s"};}};
 const chatRef:{current:ChatScrollHandle|null}={current:null};
 let selectParent=()=>{};
 function Fixture(){
  const [revision,setRevision]=React.useState(0);selectParent=()=>setRevision(value=>value+1);
  const parent=chatRef;
  const messages:any[]=Array.from({length:24},(_,i)=>({role:i%2?"assistant":"user",content:"Message "+i+" completed",
    timestamp:"2026-10-24T00:00:00Z",message_id:"m"+i}));
  const props={messages,streaming:null,cols:100,scrollChatRef:parent};
  return <InputProvider><Box width={100} height={30} flexDirection="column">
   <Box height={12} minHeight={0} flexShrink={0}>{subprocess?
    <AgentDetail {...props} rows={30} toolEvents={[]} process={{process_id:"b",display_name:"B",steps_used:1,max_steps:10} as any}/>:
    <ChatPanel {...props}/>}</Box>
   <Box maxHeight={15} flexDirection="column"><InputLayer priority={100}><BtwPanel client={client} project="p" processId="a" question="Start" answer=""
    parentBrowseRevision={revision} sidecarId="s" processes={{}} cols={100} maxBodyRows={8} onDismiss={()=>{}} onApply={()=>{}}
    onBrowseParent={delta=>parent.current?.scrollBy(delta)??delta}/></InputLayer></Box>
  </Box></InputProvider>;
 }
 const root=renderSync(<Fixture/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const views=()=>{
  const walk=(node:any):any[]=>[...(node.scrollViewportHeight!==undefined?[node]:[]),...(node.childNodes||[]).flatMap(walk)];
  return walk((instances.get(stdout) as any).rootNode);
 };
 const send=async(key:string)=>{stdin.write(key);await wait();};
 try{
  await wait();await wait();
  chatRef.current!.scrollToBottom();await wait();
  const [parent,body]=views();const bottom=parent.scrollTop;
  expect(bottom).toBeGreaterThan(0);
  // A streaming answer follows its bottom. Reach its top using normal keys,
  // without Tab or changing the active input region.
  const initialTop=body.scrollTop;
  for(let n=0;n<Math.floor(initialTop/10);n++)await send("\x1b[5~");
  for(let n=0;n<initialTop%10;n++)await send("\x1b[A");
  expect(body.scrollTop).toBe(0);expect(parent.scrollTop).toBe(bottom);
  await send("\x1b[A");expect(parent.scrollTop).toBe(bottom);expect(body.scrollTop).toBe(0);
  await send("\x1b[B");expect(parent.scrollTop).toBe(bottom);expect(body.scrollTop).toBe(1);
  await send("\x1b[5~");expect(body.scrollTop).toBe(0);expect(parent.scrollTop).toBe(bottom);
  await send("\x1b[6~");expect(parent.scrollTop).toBe(bottom);expect(body.scrollTop).toBe(10);
  selectParent();await wait();await send('\x1b[A');expect(parent.scrollTop).toBe(bottom-1);expect(body.scrollTop).toBe(10);
  const walk=(node:any):any[]=>[node,...(node.childNodes||[]).flatMap(walk)];
  const text=(node:any):string=>node.nodeName==='#text'?node.nodeValue:(node.childNodes||[]).map(text).join('');
  const discussion=walk((instances.get(stdout) as any).rootNode).find(node=>node._eventHandlers?.onClick&&text(node).includes('BTW'));
  expect(discussion).toBeDefined();discussion._eventHandlers.onClick({});await wait();
  await send('\x1b[B');expect(body.scrollTop).toBe(11);expect(parent.scrollTop).toBe(bottom-1);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
