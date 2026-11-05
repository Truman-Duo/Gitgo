import React from 'react';
import {test,expect} from 'bun:test';
import {PassThrough} from 'node:stream';
import {renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {App} from './App.js';
import {InputProvider} from '../input/runtime.js';
import type {BackendClient} from '../backend/client.js';

test('first /create and repeated entries keep empty fields separate from slash commands',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:28});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const client:BackendClient={ready:true,close(){},async callTool(op){
  if(op==='project.overview')return {projects:[]};
  if(op==='config.get')return {language:'en',verbose:false};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'Configured',model_id:'one'}]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(keys:string,delay=80)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,delay));};
 const lines=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join(''));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');
  for(const delay of [0,5,30,120]){
   await send('/create',delay);await send('\r');expect(lines().some(line=>line.includes('Create Project'))).toBe(true);
   expect(lines().find(line=>line.includes('Name *'))).toMatch(/—\s*$/u);
   await send('\r');expect(lines().find(line=>line.includes('Name *'))).toMatch(/—\s*$/u);
   expect(lines().some(line=>line.includes('/create'))).toBe(false);
   await send('intentional-'+delay);await send('\r');await send('\x1b');
  }
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},10000);

test('manual input takes over from pending verification keystrokes before /create field editing',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:28});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const client:BackendClient={ready:true,close(){},async callTool(op){if(op==='project.overview')return {projects:[]};if(op==='config.get')return {language:'en'};if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'Configured',model_id:'one'}]};return {};}};
 const root=renderSync(<InputProvider verificationInput={[{afterMs:900,input:'/edit',key:{}},{afterMs:1100,input:'',key:{return:true}}]}><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,80));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('/create');await send('\r');await send('\r');
  await new Promise(r=>setTimeout(r,1200));const s=ink.frontFrame.screen;
  const text=Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join('')).join('\n');
  expect(text).toContain('Create Project');expect(text).not.toContain('/edit');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},5000);

test('complete App keeps both editor borders in the viewport on first entry and mode changes without resize',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:28});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const client:BackendClient={ready:true,close(){},async callTool(op,args={}){
  if(op==='project.overview')return {projects:[{name:'layout',workspace:'C:/synthetic',runtime_summary:{state_available:true,daemon_online:false}}]};
  if(op==='project.enter')return {ok:true,name:'layout'};
  if(op==='config.get')return {language:'en',verbose:false,auto_compact:true};
  if(op==='runtime.status')return {daemon_online:false,state_available:true,processes:{},main_conversation:[{message_id:'u',role:'user',content:'A retained task',timestamp:'2026-10-17'}, {message_id:'a',role:'assistant',status:'completed',duration_ms:1000,content:'Retained answer',timestamp:'2026-10-17'}]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const wait=()=>new Promise(r=>setTimeout(r,130));
 const ink:any=instances.get(stdout);
 const check=()=>{const screen=ink.frontFrame.screen;const lines=Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||' ').join(''));
  const borders=lines.map((line,index)=>({line,index})).filter(({line})=>/^─{80,}\s*$/u.test(line));
  expect(borders).toHaveLength(2);expect(borders[1]!.index-borders[0]!.index).toBe(2);expect(borders[1]!.index).toBeLessThan(28);
 };
 try{ink.setAltScreenActive(true);ink.onRender();await wait();check();stdin.write('\r');await wait();await wait();check();
  stdin.write('/');await wait();check();stdin.write('\x1b');await wait();check();
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("complete App opens /processlist with no subprocesses and returns safely",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:28});let output="";stdout.on("data",(chunk:Buffer)=>output+=chunk);
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const client:BackendClient={ready:true,close(){},async callTool(op){
  if(op==='project.overview')return {projects:[{name:'empty',workspace:'C:/synthetic',runtime_summary:{state_available:true,daemon_online:false}}]};
  if(op==='project.enter')return {ok:true,name:'empty'};
  if(op==='config.get')return {language:'en',verbose:false,auto_compact:true};
  if(op==='runtime.status')return {daemon_online:false,state_available:true,processes:{},main_conversation:[]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const send=async(keys:string)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,150));};
 try{await send('');await send('\r');await send('/');await send('processlist');await send('\r');expect(output).toContain('No subprocesses.');await send('\x1b');expect(output).not.toContain('Dashboard error');}
 finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("empty project list accepts a pasted config command when a provider already exists",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:28});let output='';stdout.on('data',(chunk:Buffer)=>output+=chunk);
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const calls:any[]=[];
 const client:BackendClient={ready:true,close(){},async callTool(op){
  calls.push(op);
  if(op==='project.overview')return {projects:[]};
  if(op==='config.get')return {language:'en',verbose:false};
  if(op==='provider.status')return {active_provider:'existing',providers:[{id:'existing',name:'Existing',model_id:'old',base_url:'https://api.test',context_window:128000,max_output_tokens:4096}]};
  if(op==='provider.models')return {receipt_id:'r',models:[{id:'replacement',name:'Replacement',limits:{}}]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const send=async(keys:string)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,150));};
 try{
  await send('');await send('/config providers');await send('\r');
  expect(output).toContain('Providers');await send('\r');await send('/edit');await send('\r');
  await new Promise(r=>setTimeout(r,650));
  expect(output).toContain('Edit API connection');
  for(let i=0;i<6;i++)await send('\t');await send('\r');
  expect(output).toContain('Add models');expect(output).toContain('replacement');expect(output).not.toContain('Models available through this API');
  expect(calls.filter(op=>op==='provider.models')).toHaveLength(1);
  expect(calls).not.toContain('provider.save');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("complete App opens model and effort selectors from projects and the main conversation",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:28});let output='';stdout.on('data',(chunk:Buffer)=>output+=chunk);
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const calls:any[]=[];let selected='one',effort='low';
 const client:BackendClient={ready:true,close(){},async callTool(op,args:any={}){
  calls.push([op,args]);
  if(op==='project.overview')return {projects:[{name:'ready',workspace:'C:/ready',runtime_summary:{state_available:true}}]};
  if(op==='project.enter')return {ok:true,name:'ready'};
  if(op==='config.get')return {language:'en',verbose:false};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'API A',model_id:selected}]};
  if(op==='provider.model.options')return {provider_id:'a',provider_name:'API A',revision:'r',model_id:selected,models:[{model_id:'one'},{model_id:'two'}]};
  if(op==='provider.model.select'){selected=args.model_id;return {status:'selected'};}
  if(op==='provider.effort.options')return {provider_id:'a',provider_name:'API A',revision:'r',model_id:selected,effort,levels:['low','medium','high'],state:'declared'};
  if(op==='provider.effort.select'){effort=args.effort;return {status:'selected'};}
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const send=async(keys:string)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,100));};
 try{
  await send('');await send('/model');await send('\r');expect(output).toContain('Switch model');expect(output).toContain('API A');
  await send('\x1b[B');await send('\r');expect(selected).toBe('two');
  await send('/effort');await send('\r');await send('\x1b[C');await send('\r');expect(effort).toBe('medium');
  await send('\r');await send('/');await send('effort');await send('\r');await send('\x1b[C');await send('\r');expect(effort).toBe('high');
  await send('/');await send('model');await send('\r');
  expect(calls.filter(c=>c[0]==='provider.model.options')).toHaveLength(2);
  expect(calls.find(c=>c[0]==='provider.model.select')[1]).toEqual({provider_id:'a',model_id:'two',expected_revision:'r'});
  expect(calls.filter(c=>c[0]==='provider.effort.options')).toHaveLength(2);
  expect(calls.find(c=>c[0]==='provider.effort.select')[1]).toMatchObject({provider_id:'a',model_id:'two',effort:'medium',expected_revision:'r'});
  expect(output).not.toContain('Dashboard error');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
