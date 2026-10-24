import React from 'react';
import {test,expect} from 'bun:test';
import {PassThrough} from 'node:stream';
import {renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {App} from './App.js';
import {InputProvider} from '../input/runtime.js';
import type {BackendClient} from '../backend/client.js';

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
  const borders=lines.map((line,index)=>({line,index})).filter(({line})=>/^─{80}/u.test(line));
  expect(borders).toHaveLength(2);expect(borders[1]!.index-borders[0]!.index).toBe(2);expect(borders[1]!.index).toBeLessThan(28);
 };
 try{ink.setAltScreenActive(true);ink.onRender();await wait();check();stdin.write('\r');await wait();await wait();check();
  stdin.write('/');await wait();check();stdin.write('\x1b');await wait();check();
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
