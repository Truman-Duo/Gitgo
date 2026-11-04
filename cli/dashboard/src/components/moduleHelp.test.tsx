import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {App} from "./App.js";
function mount(){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:110,rows:36});stdout.on('data',()=>{});const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const calls:any[]=[];let model='one',effort='high';
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){calls.push([op,args]);
  if(op==='config.get')return {language:'en'};
  if(op==='project.overview')return {projects:[{name:'retained-project',workspace:'C:/synthetic',runtime_summary:{state_available:true}}]};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'API A',model_id:model}]};
  if(op==='provider.model.options')return {provider_id:'a',provider_name:'API A',model_id:model,revision:'r',models:[{model_id:'one',protocol:'openai_chat'},{model_id:'two',protocol:'openai_chat'}]};
  if(op==='provider.model.select'){model=args.model_id;return {status:'selected'};}
  if(op==='provider.effort.options')return {provider_id:'a',model_id:model,revision:'r',effort,levels:['low','high','max'],state:'declared'};
  if(op==='provider.effort.select'){effort=args.effort;return {status:'selected'};}
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(stdout);
 ink.setAltScreenActive(true);ink.onRender();
 return {calls,send:async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,110));},text(){const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join('')).join('\n');},close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("model /help is scoped and preserves selection without refetching or switching",async()=>{
 const ui=mount();try{await ui.send('');await ui.send('/model');await ui.send('\r');expect(ui.text()).not.toContain('openai_chat');expect(ui.text()).not.toContain('draft retained');
  await ui.send('/he');await ui.send('\x1b');expect(ui.text()).toContain('Switch model');expect(ui.text()).not.toContain('Model selection');await ui.send('\x1b[B');
  await ui.send('/help');await ui.send('\r');expect(ui.text()).toContain('Model selection');expect(ui.text()).toContain('idle task boundary');
  expect(ui.calls.filter(c=>c[0]==='provider.model.select')).toHaveLength(0);
  await ui.send('\x1b');expect(ui.text()).toContain('Switch model');await ui.send('\r');
  expect(ui.calls.filter(c=>c[0]==='provider.model.select')[0][1].model_id).toBe('two');expect(ui.calls.filter(c=>c[0]==='provider.model.options')).toHaveLength(1);
 }finally{ui.close();}
},10000);
test("effort /help retains its staged value and never creates a save while reading help",async()=>{
 const ui=mount();try{await ui.send('');await ui.send('/effort');await ui.send('\r');await ui.send('\x1b[D');
  await ui.send('/help');await ui.send('\r');expect(ui.text()).toContain('Reasoning effort');expect(ui.text()).toContain('persists after restart');
  expect(ui.calls.filter(c=>c[0]==='provider.effort.select')).toHaveLength(0);await ui.send('\x1b');await ui.send('\r');
  expect(ui.calls.filter(c=>c[0]==='provider.effort.select')[0][1].effort).toBe('low');expect(ui.calls.filter(c=>c[0]==='provider.effort.options')).toHaveLength(1);
 }finally{ui.close();}
},10000);
test("Provider root /help returns to the same tree with no switch or write",async()=>{
 const ui=mount();try{await ui.send('');await ui.send('/config providers');await ui.send('\r');await ui.send('\r');await ui.send('/help');await ui.send('\r');
  expect(ui.text()).toContain('Provider configuration');await ui.send('\x1b');expect(ui.text()).toContain('Automatic failover');
  expect(ui.calls.some(c=>['provider.switch','provider.save'].includes(c[0]))).toBe(false);
 }finally{ui.close();}
},10000);
