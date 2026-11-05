import React from "react";
import chalk from "chalk";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {App} from "./App.js";
import {DiscreteScale} from "./DiscreteScale.js";
import {InputProvider} from "../input/runtime.js";

function terminal(){const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:30});stdout.on("data",()=>{});const stderr:any=new PassThrough();stderr.on("data",()=>{});return {stdin,stdout,stderr};}
const rows=(ink:any)=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char??" ").join(""));};
function expectBetweenEditorAndStatus(ink:any,needle:string,additionalDividers=0){
 const lines=rows(ink),borders=lines.map((line,y)=>({line,y})).filter(p=>/^\s*─{40,}\s*$/u.test(p.line));
 expect(borders).toHaveLength(2+additionalDividers);
 const drawer=lines.findIndex(line=>line.includes(needle));
 expect(drawer,lines.join("\n")).toBeGreaterThan(borders[1]!.y);expect(drawer).toBeLessThan(lines.length-1);
 expect(lines.at(-1)!.trim().length).toBeGreaterThan(0);
}

test("the scale colors only its triangle and spreads across a bounded wider track",async()=>{
 const previous=chalk.level;chalk.level=3;const io=terminal();
 const root=renderSync(<DiscreteScale width={100} value="high" items={['low','high','max'].map(id=>({id,label:id}))}/>,{...io,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(io.stdout);
 try{
  ink.setAltScreenActive(true);ink.onRender();await new Promise(r=>setTimeout(r,90));
  const s=ink.frontFrame.screen;let colored=0,triangles=0;
  for(let y=0;y<s.height;y++)for(let x=0;x<s.width;x++){
   const cell=cellAt(s,x,y);if(!cell)continue;
   const codes=ink.stylePool.get(cell.styleId).map((style:any)=>style.code).join("");
   if(cell.char==='▲')triangles++;
   if(codes.includes('38;2;253;188;43')){colored++;expect(cell.char).toBe('▲');}
  }
  expect(triangles).toBe(1);expect(colored).toBe(1);
  expect(rows(ink).find(line=>line.includes('▲'))!.trim().length).toBe(64);
  const labels=rows(ink).find(line=>line.includes('low')&&line.includes('max'))!;
  expect(labels.indexOf('low')).toBe(0);expect(labels.indexOf('max')+3).toBe(64);
  expect(rows(ink).find(line=>line.includes('▲'))!.indexOf('▲')).toBe(32);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();chalk.level=previous;}
});

test("five effort levels balance dynamically across a narrower track with the last marker at the right edge",async()=>{
 const io=terminal(),ids=['low','medium','high','xhigh','max'];
 const root=renderSync(<DiscreteScale width={41} value="max" items={ids.map(id=>({id,label:id}))}/>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 try{
  ink.setAltScreenActive(true);ink.onRender();await new Promise(r=>setTimeout(r,90));
  const track=rows(ink).find(line=>line.includes('▲'))!,labels=rows(ink).find(line=>line.includes('medium'))!;
  expect(track.indexOf('▲')).toBe(40);expect(labels.indexOf('low')).toBe(0);expect(labels.indexOf('max')+3).toBe(41);
  const centers=ids.map(id=>labels.indexOf(id)+Math.floor(id.length/2)),gaps=centers.slice(1).map((x,i)=>x-centers[i]!);
  expect(Math.max(...gaps)-Math.min(...gaps)).toBeLessThanOrEqual(1);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

test("effort and running-model interruption use the footer without replacing the project or conversation",async()=>{
 const io=terminal(),calls:any[]=[];let effort='';
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){
  calls.push([op,args]);if(op==='project.overview')return {projects:[{name:'visible-project',workspace:'C:/synthetic',runtime_summary:{state_available:true}}]};
  if(op==='config.get')return {language:'en',verbose:false};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'API A',model_id:'one'}]};
  if(op==='provider.effort.options')return {provider_id:'a',provider_name:'API A',model_id:'one',revision:'r',effort,default_effort:'high',effective_effort:effort||'high',levels:['low','high','max'],state:'declared'};
  if(op==='provider.effort.select'){effort=args.effort;return {status:'selected',effective_from:'next_model_request'};}
  if(op==='project.enter')return {ok:true,name:'visible-project'};
  if(op==='runtime.status')return {state_available:true,daemon_online:true,processes:{a:{process_id:'a',parent_id:null,actor_kind:'supervisor',status:'running',created_at:'2026-11-04T00:00:00'}},main_conversation:[{message_id:'a1',role:'assistant',status:'completed',content:'Retained conversation text',timestamp:'2026-11-04T00:00:00'}]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,110));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('/effort');await send('\r');
  expect(rows(ink).some(line=>line.includes('visible-project'))).toBe(true);
  expect(rows(ink).findIndex(line=>line.includes('Faster'))).toBeGreaterThan(18);
  expectBetweenEditorAndStatus(ink,'Faster');expect(rows(ink).at(-1)).toContain('projects');
  expect(rows(ink).some(line=>line.includes('Effort ·'))).toBe(false);
  expect(rows(ink).some(line=>line.includes('default'))).toBe(false);
  await send('\x1b[C');await send('\r');expect(effort).toBe('max');
  expect(rows(ink).some(line=>line.includes('applies from the next model request'))).toBe(true);
  await send('/effort');await send('\r');await send('\x1b');
  expect(calls.filter(c=>c[0]==='provider.effort.select')).toHaveLength(1);
  await send('\r');await send('');expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  await send('/');await send('effort');await send('\r');expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  expect(rows(ink).findIndex(line=>line.includes('Faster'))).toBeGreaterThan(18);
  expectBetweenEditorAndStatus(ink,'Faster');expect(rows(ink).at(-1)).toContain('●');
  await send('\x1b[D');await send('\r');expect(effort).toBe('high');expect(calls.filter(c=>c[0]==='provider.effort.select')).toHaveLength(2);
  await send('\x1b');expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  expect(rows(ink).findIndex(line=>line.includes('Interrupt the main process'))).toBeGreaterThan(18);
  expectBetweenEditorAndStatus(ink,'Interrupt the main process');expect(rows(ink).at(-1)).toContain('●');
  await send('\x1b');expect(calls.some(c=>c[0]==='process.stop'||c[0]==='host.cancel')).toBe(false);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
},15000);

test("the complete config tree returns API form to provider choices before returning to the provider tab",async()=>{
 const io=terminal();
 const client:any={ready:true,close(){},async callTool(op:string){if(op==='project.overview')return {projects:[{name:'visible-project',workspace:'C:/synthetic',runtime_summary:{state_available:true}}]};if(op==='config.get')return {language:'en'};if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'Existing',model_id:'one'}]};return {};}};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,100));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('/config providers');await send('\r');await send('\r');await send('/new');await send('\r');await send('\r');
  expect(rows(ink).some(line=>line.includes('Configure API connection'))).toBe(true);
  await send('\x1b');expect(rows(ink).some(line=>line.includes('Choose Provider'))).toBe(true);expect(rows(ink).some(line=>line.includes('Automatic failover'))).toBe(false);
  await send('\x1b');expect(rows(ink).some(line=>line.includes('Automatic failover'))).toBe(true);
  await send('\x1b');expect(rows(ink).some(line=>line.includes('Providers'))).toBe(true);
  await send('\x1b');expect(rows(ink).some(line=>line.includes('visible-project'))).toBe(true);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
},10000);

test("undo shares the footer drawer, preserves conversation/editor/status, and only explicit confirmation commits",async()=>{
 const io=terminal(),calls:any[]=[];let finish:any;
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){
  calls.push([op,args]);
  if(op==='project.overview')return {projects:[{name:'visible-project',workspace:'C:/synthetic',runtime_summary:{state_available:true}}]};
  if(op==='config.get')return {language:'zh',verbose:false};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'API A',model_id:'one'}]};
  if(op==='project.enter')return {ok:true,name:'visible-project'};
  if(op==='runtime.status')return {state_available:true,daemon_online:true,processes:{a:{process_id:'a',parent_id:null,actor_kind:'supervisor',status:'completed',created_at:'2026-11-04T00:00:00'}},main_conversation:[{message_id:'a1',role:'assistant',status:'completed',content:'Retained conversation text',timestamp:'2026-11-04T00:00:00'}]};
  if(op==='runtime.undo.preview')return {process_id:'a',checkpoint_id:'checkpoint-1',turn_preview:'用户上一轮的目标\n'+('较长的内容'.repeat(200)),warning:'Conversation context will rewind. Files, commits, provider calls, and other external effects are retained and remain auditable.'};
  if(op==='runtime.undo')return new Promise(resolve=>finish=()=>resolve({status:'committed'}));
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,110));};
 const commits=()=>calls.filter(c=>c[0]==='runtime.undo');
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('\r');await send('');
  expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  await send('/');await send('undo');await send('\r');expectBetweenEditorAndStatus(ink,'会话撤回');
  expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);expect(rows(ink).at(-1)).toContain('●');
  expect(rows(ink).join('\n')).not.toContain('轮次：');expect(rows(ink).join('\n')).not.toContain('用户上一轮的目标');
  expect(rows(ink).join('\n')).not.toContain('仅撤回对话上下文');expect(rows(ink).join('\n')).not.toContain('Conversation context will rewind');
  expect(Boolean(ink.cursorDeclaration?.visible)).toBe(false);expect(commits()).toHaveLength(0);
  await send('/help');await send('\r');expectBetweenEditorAndStatus(ink,'使用说明',1);expect(commits()).toHaveLength(0);
  expect(rows(ink).join('\n')).toContain('保留工作区改动与已发生的外部效果');
  await send('\x1b');expectBetweenEditorAndStatus(ink,'会话撤回');await send('\x1b');expect(commits()).toHaveLength(0);
  expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  for(const [width,height]of [[52,20],[100,30]]){
   io.stdout.columns=width;io.stdout.rows=height;io.stdout.emit('resize');await send('');await send('/');await send('undo');await send('\r');
   expectBetweenEditorAndStatus(ink,'会话撤回');expect(rows(ink).some(line=>line.includes('保留当前对话'))).toBe(true);
   await send('\x1b');expect(commits()).toHaveLength(0);
  }
  await send('/');await send('undo');await send('\r');await send('\r');await send('\r');expect(commits()).toHaveLength(1);expect(commits()[0][1]).toMatchObject({project:'visible-project',process_id:'a',checkpoint_id:'checkpoint-1'});
  finish();await send('');expect(rows(ink).some(line=>line.includes('会话撤回'))).toBe(false);expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
},15000);

test("model selection stays in a bounded footer in both scenes and busy rejection preserves the live conversation",async()=>{
 const previous=chalk.level;chalk.level=3;
 const io=terminal(),calls:any[]=[];let model='model-01',entered=false;
 const models=Array.from({length:24},(_,i)=>({model_id:'model-'+String(i+1).padStart(2,'0')}));
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){
  calls.push([op,args]);if(op==='project.overview')return {projects:[{name:'visible-project',workspace:'C:/synthetic',runtime_summary:{state_available:true}}]};
  if(op==='config.get')return {language:'en'};
  if(op==='provider.status')return {active_provider:'a',providers:[{id:'a',name:'API A',model_id:model}]};
  if(op==='provider.model.options')return {provider_id:'a',provider_name:'API A',revision:'r',model_id:model,models};
  if(op==='provider.model.select'){if(entered)throw {code:'PROVIDER_SWITCH_BUSY'};model=args.model_id;return {status:'selected'};}
  if(op==='project.enter'){entered=true;return {ok:true,name:'visible-project'};}
  if(op==='runtime.status')return {state_available:true,daemon_online:true,processes:{a:{process_id:'a',parent_id:null,actor_kind:'supervisor',status:'running',created_at:'2026-11-04T00:00:00'}},main_conversation:[{message_id:'a1',role:'assistant',status:'completed',content:'Retained conversation text',timestamp:'2026-11-04T00:00:00'}]};
  return {};
 }};
 const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string,delay=100)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,delay));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('/model');await send('\r');
  expect(rows(ink).some(line=>line.includes('visible-project'))).toBe(true);
  expect(rows(ink).findIndex(line=>line.includes('Switch model'))).toBeGreaterThan(10);
  expectBetweenEditorAndStatus(ink,'Switch model');expect(rows(ink).at(-1)).toContain('projects');
  expect(rows(ink).filter(line=>/model-\d+/u.test(line))).toHaveLength(8);
  const selectedY=rows(ink).findIndex(line=>line.includes('model-01')),selectedX=rows(ink)[selectedY]!.indexOf('model-01');
  const selectedCell=cellAt(ink.frontFrame.screen,selectedX,selectedY)!;
  const selectedCodes=ink.stylePool.get(selectedCell.styleId).map((s:any)=>s.code).join('');
  expect(selectedCodes).toContain('38;2;240;240;240');expect(selectedCodes).toContain('48;2;42;42;42');expect(selectedCodes).not.toContain('38;2;63;185;80');
  io.stdout.rows=18;io.stdout.emit('resize');await send('');
  expect(rows(ink).some(line=>line.includes('visible-project'))).toBe(true);
  expect(rows(ink).filter(line=>/model-\d+/u.test(line))).toHaveLength(1);
  io.stdout.rows=30;io.stdout.emit('resize');await send('');
  await send('\x1b[B');await send('\r');expect(model).toBe('model-02');
  await send('/model');await send('\r');for(let i=0;i<30;i++)await send('\x1b[B',20);
  expect(rows(ink).some(line=>/^\d+\/\d+$/u.test(line.trim()))).toBe(false);expect(rows(ink).filter(line=>/model-\d+/u.test(line))).toHaveLength(8);
  await send('\r');expect(model).toBe('model-24');
  await send('\r');await send('');await send('/');await send('model');await send('\r');
  expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  expect(rows(ink).findIndex(line=>line.includes('Switch model'))).toBeGreaterThan(10);
  expectBetweenEditorAndStatus(ink,'Switch model');expect(rows(ink).at(-1)).toContain('●');
  await send('\x1b[A');await send('\r');expect(model).toBe('model-24');
  expect(rows(ink).some(line=>line.includes('PROVIDER_SWITCH_BUSY'))).toBe(true);
  expect(rows(ink).some(line=>line.includes('Retained conversation text'))).toBe(true);
  await send('\x1b');expect(calls.some(c=>c[0]==='process.stop'||c[0]==='host.cancel')).toBe(false);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();chalk.level=previous;}
},10000);
