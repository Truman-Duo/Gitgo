import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {ToolCallDisplay} from "./ToolCallDisplay.js";
import {DiffView} from "./DiffView.js";
import {GeneralTab} from "./config/GeneralTab.js";
import {InputProvider} from "../input/runtime.js";
import {useGeneralConfig} from "../hooks/useGeneralConfig.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {DiffLayoutContext} from "../theme/diffLayoutContext.js";
import {parseUnifiedDiff} from "../utils/diff.js";
import type {BackendClient} from "../backend/client.js";

const patch="--- a/sample.py\n+++ b/sample.py\n@@ -10,1 +20,1 @@\n-old_value = 1\n+new_value = 2\n";
function terminal(){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:40});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});
 return {stdin,stdout,stderr};
}
function lines(stdout:any){const ink:any=instances.get(stdout),screen=ink.frontFrame.screen;return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char||' ').join(''));}
const wait=()=>new Promise(r=>setTimeout(r,75));

test('General saves split/unified once and compact tool diffs retain the shared frame and update immediately',async()=>{
 const io=terminal(),saved:any[]=[];let layout='split';
 const client:BackendClient={ready:true,close(){},async callTool(op,args={}){
  if(op==='config.get')return {diff_layout:layout};
  if(op==='config.terminals')return {options:[],warnings:[]};
  if(op==='config.set'){saved.push(args);layout=String(args.value);return {ok:true};}
  return {};
 }};
 function Fixture(){const config=useGeneralConfig(client),input=useTextInput();return <InputProvider><DiffLayoutContext.Provider value={config.diffLayout}><Box flexDirection="column" height={40}>
  <GeneralTab client={client} project="test" cmdInput={input} initialSetting="diff_layout" contentFocused onFooter={()=>{}} report={()=>{}} shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/>
  <ToolCallDisplay compactPreview width={96} tool={{tool_name:'write_file',target:'sample.py',state:'completed',allowed:true,status_label:'OK',duration_ms:1,timestamp:'',diff:patch}}/>
 </Box></DiffLayoutContext.Provider></InputProvider>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();let frame=lines(io.stdout);
  expect(frame.some(line=>line.includes('old_value')&&line.includes('new_value'))).toBe(true);
  expect(frame.some(line=>line.includes('┌─'))).toBe(true);expect(frame.some(line=>line.includes('└─'))).toBe(true);
  io.stdin.write('\r');await wait();io.stdin.write('\x1b[C');await wait();io.stdin.write('\x1b');await wait();
  expect(saved).toHaveLength(0);expect(lines(io.stdout).some(line=>line.includes('old_value')&&line.includes('new_value'))).toBe(true);
  io.stdin.write('\r');await wait();io.stdin.write('\x1b[C');await wait();io.stdin.write('\r');await wait();await wait();
  expect(saved).toEqual([{key:'diff_layout',value:'unified'}]);frame=lines(io.stdout);
  const old=frame.findIndex(line=>line.includes('old_value')),next=frame.findIndex(line=>line.includes('new_value'));
  expect(old).toBeGreaterThanOrEqual(0);expect(next).toBe(old+1);
  expect(frame[old]).toMatch(/10\s+- old_value/);expect(frame[next]).toMatch(/20\s+\+ new_value/);
  expect(frame.filter(line=>line.includes('sample.py'))).toHaveLength(1);
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(stream=>stream.destroy());}
});

test('compact diffs bound content inside a complete frame while full and narrow layouts keep real line numbers',async()=>{
 const io=terminal();const many='--- a/sample.py\n+++ b/sample.py\n@@ -100,8 +200,8 @@\n'+Array.from({length:8},(_,i)=>'-old_'+i+'\n').join('')+Array.from({length:8},(_,i)=>'+new_'+i+'\n').join('');
 const files=parseUnifiedDiff(many);
 const draw=(width:number,layout:'split'|'unified',bounded:boolean)=><Box height={40}><DiffView files={files} width={width} layout={layout} maxRows={bounded?6:undefined}/></Box>;
 const root=renderSync(draw(96,'unified',true),{...io,patchConsole:false,exitOnCtrlC:false});
 try{
  await wait();let frame=lines(io.stdout);expect(frame.join('\n')).toContain('additional changes omitted');expect(frame.join('\n')).not.toContain('old_7');expect(frame.some(line=>line.includes('└─'))).toBe(true);
  root.rerender(draw(96,'unified',false));await wait();frame=lines(io.stdout);expect(frame.join('\n')).not.toContain('additional changes omitted');expect(frame.some(line=>/207\s+\+ new_7/.test(line))).toBe(true);
  root.rerender(draw(40,'split',false));await wait();frame=lines(io.stdout);expect(frame.findIndex(line=>line.includes('old_0'))).toBeLessThan(frame.findIndex(line=>line.includes('new_0')));
 }finally{root.unmount();root.cleanup();Object.values(io).forEach(stream=>stream.destroy());}
});
