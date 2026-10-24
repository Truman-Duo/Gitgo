import React,{createRef} from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync,type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {VirtualConversation} from "./VirtualConversation.js";
import type {ChatMessage,PendingDecision,StreamingRow} from "../types.js";
import {toolPreview} from "../chat/toolPreview.js";
import {TurnTimeline} from "./TurnTimeline.js";
import {animateNumber} from "../theme/motion.js";

const wait=(ms=80)=>new Promise(r=>setTimeout(r,ms));
function terminal(){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:28});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 return {stdin,stdout,stderr};
}
function allText(node:any):string{return node.nodeValue||node.childNodes?.map(allText).join('')||'';}
function nodes(node:any):any[]{return [node,...(node.childNodes||[]).flatMap(nodes)];}
function state(stdout:any){const ink:any=instances.get(stdout);const screen=ink.frontFrame.screen;return {ink,text:Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||' ').join('')).join('\n')};}
const decision=(id:string):PendingDecision=>({task_id:'task',process_id:'main',decision_id:id,kind:'clarification',question:`question-${id}`,why_user_must_decide:'Please choose a scope',options:[],allow_free_form:true});
const user:ChatMessage={role:'user',content:'Inspect this file',message_id:'user-1',timestamp:'2026-10-17T00:00:00Z'};
const card=(id:string,answered=false):ChatMessage=>({role:'assistant',content:'',timestamp:user.timestamp,message_id:`card-${id}`,decision:decision(id),decision_answer:answered?'yes':null});

test('new decision preserves earlier open cards, including virtual unmount and remount',async()=>{
 const io=terminal(),scroll=createRef<ScrollBoxHandle>();
 const draw=(messages:ChatMessage[],pending?:PendingDecision)=><InputProvider><Box height={28} width={100}><VirtualConversation messages={messages} streaming={null} contentWidth={94} scrollRef={scroll} pendingDecision={pending}/></Box></InputProvider>;
 const root=renderSync(draw([user,card('one')],decision('one')),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();root.rerender(draw([user,card('one',true),card('two')],decision('two')));await wait();
  expect(allText(state(io.stdout).ink.rootNode)).toContain('question-one');
  expect(allText(state(io.stdout).ink.rootNode)).toContain('question-two');
  root.rerender(draw([user,card('one',true),card('two',true)]));await wait();
  const click=nodes(state(io.stdout).ink.rootNode).find(n=>n._eventHandlers?.onClick&&allText(n).startsWith('Decision answered'));
  expect(click).toBeDefined();click._eventHandlers.onClick({});await wait(230);
  expect(allText(state(io.stdout).ink.rootNode)).not.toContain('question-one');
  click._eventHandlers.onClick({});await wait(230);
  expect(state(io.stdout).text).toContain('question-one');
  expect(allText(state(io.stdout).ink.rootNode)).toContain('question-one');
  const long=[user,card('one',true),card('two',true),...Array.from({length:65},(_,i):ChatMessage=>({role:i%2?'assistant':'user',message_id:`later-${i}`,content:`later ${i}\nmore lines\ncontext`,timestamp:user.timestamp}))];
  root.rerender(draw(long));await wait();scroll.current?.scrollToBottom();await wait(200);
  expect(allText(state(io.stdout).ink.rootNode)).not.toContain('question-one');
  scroll.current?.scrollTo(0);await wait(250);
  expect(allText(state(io.stdout).ink.rootNode)).toContain('question-one');
  expect(allText(state(io.stdout).ink.rootNode)).toContain('question-two');
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});

test('live tool preview and turn guide fold gradually into the final reply without a trace read',async()=>{
 const io=terminal(),scroll=createRef<ScrollBoxHandle>(),calls:any[]=[];
 const stream:StreamingRow={text:'',reasoning:'private reasoning must stay hidden',timestamp:new Date().toISOString(),tools:[{tool_name:'bash',tool_call_id:'c',target:'python check.py',result_text:Array.from({length:9},(_,i)=>`preview-line-${i}`).join('\n'),state:'completed',allowed:true,status_label:'OK',duration_ms:120,timestamp:''}],notices:[]};
 const client:any={ready:true,close(){},async callTool(op:any,args:any){calls.push({op,args});return {};}};
 const draw=(messages:ChatMessage[],streaming:StreamingRow|null)=><InputProvider><Box height={28} width={100}><VirtualConversation client={client} project="sample" messages={messages} streaming={streaming} contentWidth={94} scrollRef={scroll}/></Box></InputProvider>;
 const root=renderSync(draw([user],stream),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();const before=state(io.stdout).text;
  expect(before).toContain('┏');expect(before).toContain('│');expect(before).not.toContain('┗');
  expect(before.split('\n').find(line=>line.includes('Inspect this file'))?.[0]).toBe('┏');
  expect(before).toContain('python check.py');expect(before).toContain('preview-line-0');expect(before).not.toContain('preview-line-8');expect(before).not.toContain('private reasoning');
  const outcome:ChatMessage={role:'assistant',content:'Verified result\nLast line of answer',message_id:'outcome',timestamp:new Date().toISOString(),status:'completed',trace_id:'task',duration_ms:120,tools:stream.tools};
  root.rerender(draw([{...user,message_id:"persisted-user-1"},outcome],null));await wait(50);
  const during=state(io.stdout).text;expect(during).toContain('preview-line-0');expect(during).toContain('Verified result');
  await wait(270);const after=state(io.stdout).text;
  expect(after).not.toContain('preview-line-0');expect(after).toContain('Verified result');expect(after).toContain('┗');expect(after.split('\n').find(line=>line.includes('Last line of answer'))?.[0]).toBe('┗');expect(calls).toHaveLength(0);
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});

test('verbose composite children retain their explicit parent label and bounded previews strip controls',async()=>{
 const preview=toolPreview({tool_name:'bash',target:'cmd',allowed:true,status_label:'OK',duration_ms:1,timestamp:'',result_text:'\x1b[31mhello\x1b[0m\nsecond\nthird\nfourth\nfifth\nsixth'},40);
 expect(preview.lines.map(l=>l.text)).toEqual(['cmd','hello','second','third','fourth']);expect(preview.more).toBe(true);
 expect(toolPreview({tool_name:'bash',target:'first\nsecond\nthird',allowed:true,status_label:'OK',duration_ms:0,timestamp:''},40).more).toBe(true);
 const io=terminal();const root=renderSync(<InputProvider><Box height={28}><TurnTimeline width={94} verbose activity={[{kind:'tool',toolIndex:0}]} tools={[{tool_name:'read_file',allowed:true,status_label:'OK',timestamp:'',duration_ms:20,state:'completed',target:'child.txt',composite_tool:'inspect_bundle',step_id:'source',execution_id:'exec',result_text:'Nested file content'}]} notices={[]}/></Box></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{await wait();const text=state(io.stdout).text;expect(text).toContain('inspect_bundle / source');expect(text).toContain('Nested file content');expect(text).toContain('│');}finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});

test('motion cancellation prevents frames after the owner unmounts',async()=>{
 const values:number[]=[];const stop=animateNumber(20,0,v=>values.push(v),{durationMs:180,intervalMs:20});await wait(45);stop();const count=values.length;expect(count).toBeGreaterThan(0);expect(values[0]).toBeLessThan(20);expect(values[0]).toBeGreaterThan(0);await wait(200);expect(values.length).toBe(count);
});


test('verbose renders interleaved phases in arrival order while compact mode keeps public progress',async()=>{
 const io=terminal();
 const activity:NonNullable<StreamingRow['activity']>=[
  {kind:'progress',text:'First public update',visibility:'public'},
  {kind:'reasoning',text:'First private thought'},
  {kind:'tool',toolIndex:0},
  {kind:'reasoning',text:'Second private thought'},
  {kind:'progress',text:'Second public update',visibility:'public'},
  {kind:'tool',toolIndex:1},
 ];
 const tools:NonNullable<StreamingRow['tools']>=[
  {tool_name:'read_file',target:'phase-one.txt',allowed:true,status_label:'OK',state:'completed',timestamp:'',duration_ms:20},
  {tool_name:'search_text',target:'phase-two',allowed:true,status_label:'OK',state:'running',timestamp:'',duration_ms:0},
 ];
 const draw=(verbose:boolean)=><InputProvider><Box height={28}><TurnTimeline activity={activity} tools={tools} notices={[]} width={94} verbose={verbose} active/></Box></InputProvider>;
 const root=renderSync(draw(true),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();const text=state(io.stdout).text;
  const positions=['First public update','First private thought','read_file','Second private thought','Second public update','search_text'].map(label=>text.indexOf(label));
  expect(positions.every(position=>position>=0)).toBe(true);
  expect(positions).toEqual([...positions].sort((a,b)=>a-b));
  expect(text.match(/Thinking/g)).toHaveLength(2);
  root.rerender(draw(false));await wait();const compact=state(io.stdout).text;
  expect(compact).toContain('First public update');expect(compact).toContain('Second public update');
  expect(compact).not.toContain('private thought');expect(compact).toContain('read_file');expect(compact).toContain('search_text');
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(s=>s.destroy());}
});
