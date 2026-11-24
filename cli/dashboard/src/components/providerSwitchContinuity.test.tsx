import React from 'react';
import {test,expect} from 'bun:test';
import {PassThrough} from 'node:stream';
import {renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {App} from './App.js';
import {InputProvider} from '../input/runtime.js';

// Full App, normal commands and production input routing. Frame assertions
// establish continuity, not real-terminal visual acceptance.
test('API/model changes and full-screen config preserve the same project reader and history position',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:90,rows:28});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 let active='gpt',model='gpt-model';const calls:string[]=[];
 const providers=[{id:'gpt',name:'A GPT',model_id:'gpt-model'},{id:'deepseek',name:'B DeepSeek',model_id:'deepseek-model'}];
 const messages=Array.from({length:70},(_,i)=>[
  {role:'user',message_id:`u${i}`,turn_id:`t${i}`,content:`USER ${i}\nSaved prompt detail ${i}`,timestamp:'now'},
  {role:'assistant',message_id:`a${i}`,turn_id:`t${i}`,content:`ANSWER ${i}\n${'Full saved answer line\n'.repeat(5)}`,status:'completed',final:true,timestamp:'now'},
 ]).flat();
 const client:any={ready:true,close(){},subscribeEvents(){return()=>{};},async callTool(op:string,args:any={}){
  calls.push(op);
  if(op==='config.get')return {language:'en',verbose:false};
  if(op==='project.overview')return {projects:[{name:'saved',workspace:'C:/saved',project_id:'saved-id',runtime_summary:{state_available:true}}]};
  if(op==='project.enter')return {ok:true};
  if(op==='provider.status')return {providers,active_provider:active,failover_enabled:false};
  if(op==='runtime.status')return {daemon_online:true,processes:{},main_conversation:messages,agent_conversations:{},storage:{level:'ok'}};
  if(op==='provider.switch'){active=args.provider_id;model=active==='gpt'?'gpt-model':'deepseek-model';return {active_provider:active,runtime_reload:false};}
  if(op==='provider.model.options')return {provider_id:active,revision:'revision',model_id:model,models:[{model_id:model},{model_id:'alternate'}]};
  if(op==='provider.model.select'){model=args.model_id;return {status:'selected',runtime_reload:false};}
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
 const wait=()=>new Promise(r=>setTimeout(r,110));
 const key=async(s:string)=>{stdin.write(s);await wait();};
 const frame=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join('')).join('\n');};
 const scrollers=(node:any):any[]=>[...(node.scrollHeight>100?[node]:[]),...(node.childNodes||[]).flatMap(scrollers)];
 try{await wait();await key('\r');await wait();expect(frame()).toContain('Chat — saved');
  await key('\x1b[5~');await key('\x1b[5~');await wait();
  const reader=scrollers(ink.rootNode)[0];expect(reader).toBeDefined();expect(reader.scrollTop).toBeGreaterThan(0);
  const lines=frame().split('\n'),userRow=lines.findIndex(line=>line.includes('USER '));
  expect(userRow).toBeGreaterThanOrEqual(0);
  const x=lines[userRow]!.indexOf('USER ')-2;
  stdin.write(`\x1b[<0;${x+1};${userRow+1}M\x1b[<0;${x+1};${userRow+1}m`);await wait();
  expect(frame()).toContain('Saved prompt detail');
  const before=reader.renderedScrollTop,height=reader.scrollHeight;
  await key('/');await key('config providers');await key('\r');await key('\r');
  expect(frame()).toContain('B DeepSeek');
  const readsBeforeSwitch=calls.filter(op=>op==='runtime.status').length;
  await key('\x1b[B'.repeat(4));await key('\r');
  expect(calls.filter(op=>op==='runtime.status')).toHaveLength(readsBeforeSwitch);
  expect(active).toBe('deepseek');await key('\x1b');await key('\x1b');await wait();
  expect(frame()).toContain('Chat — saved');expect(scrollers(ink.rootNode)[0]).toBe(reader);
  expect(frame()).toContain('Saved prompt detail');
  expect(reader.scrollHeight).toBe(height);expect(Math.abs(reader.renderedScrollTop-before)).toBeLessThanOrEqual(1);
  await key('/');await key('model');await key('\r');await key('\x1b[B');await key('\r');await wait();
  expect(calls).toContain('provider.model.select');expect(frame()).toContain('Chat — saved');
  expect(scrollers(ink.rootNode)[0]).toBe(reader);expect(Math.abs(reader.renderedScrollTop-before)).toBeLessThanOrEqual(1);
  expect(calls.filter(op=>op==='runtime.chat')).toHaveLength(0);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);
