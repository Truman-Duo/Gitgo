import React from 'react';
import {test,expect} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box,renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {ChatPanel} from './ChatPanel.js';
import {TextInput} from './TextInput.js';
import {InputProvider} from '../input/runtime.js';
import {AgentDetail} from './AgentDetail.js';
import {BtwPanel} from './BtwPanel.js';

for(const width of [34,80])for(const child of [false,true])test(`conversation pane keeps scrollbar inside physical width ${width} (child=${child})`,async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:width,rows:20});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const props={cols:width,project:'sample',messages:Array.from({length:100},(_,i)=>({role:'user' as const,content:'row '+i,message_id:String(i),timestamp:'now'})),streaming:null,scrollChatRef:{current:null}};
 const root=renderSync(<InputProvider><Box width={width} height={20} flexDirection='column'>{child?
  <AgentDetail {...props} rows={20} toolEvents={[]} process={{process_id:'b',display_name:'B',status:'completed',steps_used:1,max_steps:10} as any}/>
  :<ChatPanel {...props}/>}</Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 try{await new Promise(r=>setTimeout(r,90));const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
  stdin.write(`\x1b[<35;${width};11M`);await new Promise(r=>setTimeout(r,60));const s=ink.frontFrame.screen;
  const last=Array.from({length:20},(_,y)=>cellAt(s,width-1,y)?.char||' ').join('');
  expect(last).toContain('┃');expect(last).toContain('│');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test('BTW follow-up exposes its native caret before typing, after submission and while the answer is pending',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:80,rows:18});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});let count=0,release:(v:any)=>void=()=>{};
 const client:any={callTool:async()=>{count++;if(count===1)return {answer:'first answer',sidecar_id:'side'};return new Promise(resolve=>release=resolve);}};
 const root=renderSync(<InputProvider><Box height={18} width={80}><BtwPanel client={client} project='p' processId='a' question='first'
  answer='' sidecarId='' processes={{}} cols={80} onDismiss={()=>{}} onApply={()=>{}}/></Box></InputProvider>,
 {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),pause=()=>new Promise(r=>setTimeout(r,70));
 try{await pause();ink.onRender();expect(ink.cursorDeclaration?.visible).toBe(true);
  stdin.write('next');await pause();ink.onRender();expect(ink.cursorDeclaration?.relativeX).toBe(4);
  stdin.write('\r');await pause();ink.onRender();expect(count).toBe(2);expect(ink.cursorDeclaration?.visible).toBe(true);expect(ink.cursorDeclaration?.relativeX).toBe(0);
  release({answer:'second answer',sidecar_id:'side'});await pause();ink.onRender();expect(ink.cursorDeclaration?.visible).toBe(true);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test('empty and populated focused inputs share a native blinking bar and empty placeholder caret',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:60,rows:10});let writes='';stdout.on('data',(c:any)=>writes+=String(c));
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const draw=(value:string)=><Box width={60} height={10}><TextInput value={value} cursorOffset={value.length} placeholder='Follow-up'/></Box>;
 const root=renderSync(draw(''),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);
 try{ink.onRender();await new Promise(r=>setTimeout(r,40));expect(ink.cursorDeclaration?.visible).toBe(true);expect(writes).toContain('\x1b[5 q');
  root.rerender(draw('answer'));ink.onRender();await new Promise(r=>setTimeout(r,40));expect(ink.cursorDeclaration?.visible).toBe(true);
  root.rerender(draw(''));ink.onRender();await new Promise(r=>setTimeout(r,40));expect(ink.cursorDeclaration?.visible).toBe(true);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
