import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync,Box} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {DocumentEditorScene} from "./DocumentEditorScene.js";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";

test("full document editor reveals the cursor in a bounded viewport and retains text after failed saving",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{columns:38,rows:14});let output='';stdout.on('data',(b:Buffer)=>output+=b);
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const footers:any[]=[],saved:string[]=[];let cancelled=0;
 const original=Array.from({length:40},(_,i)=>`line-${i}`).join('\n');
 const root=renderSync(<InputProvider><Box width={38} height={14} flexDirection="column"><DocumentEditorScene title="JSON" initialValue={original} onFooter={f=>footers.push(f)} onCancel={()=>cancelled++} onSave={text=>{saved.push(text);throw Error('SYNTHETIC_VALIDATION');}}/></Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const send=async(s:string)=>{stdin.write(s);await new Promise(r=>setTimeout(r,75));};
 try{
  await send('');expect(output).toContain('line-0');expect(output).not.toContain('line-39');expect(footers.at(-1).hidden).toBe(true);
  await send('\x1b[F');expect(output).toContain('line-39');await send('\r');await send('tail');await send('\x13');expect(saved[0]).toBe(original+'\ntail');expect(output).toContain('SYNTHETIC_VALIDATION');
  await send('\x13');expect(saved[1]).toBe(saved[0]);await send('\x1b');expect(cancelled).toBe(1);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("mouse wheel browses independently, clamps at both edges, and keyboard motion reveals the caret again",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:48,rows:12});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const saved:string[]=[];
 const original=Array.from({length:40},(_,i)=>`row-${String(i).padStart(2,'0')}`).join('\n');
 const root=renderSync(<InputProvider><Box width={48} height={12} flexDirection="column"><DocumentEditorScene title="JSON" initialValue={original} onFooter={()=>{}} onCancel={()=>{}} onSave={text=>{saved.push(text);}}/></Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(s:string)=>{stdin.write(s);await new Promise(r=>setTimeout(r,80));};
 const lines=()=>{const screen=ink.frontFrame.screen;return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char || ' ').join(''));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');expect(lines()[1]).toContain('row-00');
  await send('\x1b[<65;5;5M');expect(lines()[1]).toContain('row-03');expect(ink.cursorDeclaration.visible).toBe(false);
  await send('\x1b[<65;5;5M'.repeat(100));expect(lines()[10]).toContain('row-39');
  await send('\x1b[<65;5;5M');expect(lines()[10]).toContain('row-39');
  await send('\x1b[<64;5;5M');expect(lines()[1]).toContain('row-27');
  await send('\x1b[C');expect(lines()[1]).toContain('row-00');expect(ink.cursorDeclaration.visible).toBe(true);
  await send('\x1b[<64;5;5M'.repeat(100));expect(lines()[1]).toContain('row-00');
  await send('\x13');expect(saved).toEqual([original]);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

test("left/right across wrapping and real newlines preserve whitespace, CJK, emoji and insertion position",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:22,rows:12});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});let saved='';
 // The reserved caret cell leaves 21 content cells; the boundary falls inside a string.
 const original='  "name": "甲乙丙丁戊👩‍💻 é tail"\n  "n": 42';
 const root=renderSync(<InputProvider><Box width={22} height={12} flexDirection="column"><DocumentEditorScene title="JSON" syntaxLanguage="json" initialValue={original} onFooter={()=>{}} onCancel={()=>{}} onSave={text=>{saved=text;}}/></Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout),send=async(s:string)=>{stdin.write(s);await new Promise(r=>setTimeout(r,65));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');
  for(let i=0;i<16;i++)await send('\x1b[C');
  expect(ink.cursorDeclaration).toMatchObject({relativeX:0,relativeY:1});
  await send('\x1b[D');expect(ink.cursorDeclaration).toMatchObject({relativeX:19,relativeY:0});
  await send('\x1b[C');await send('X');
  await send('\x1b[F');await send('\x1b[D');await send('\x1b[C');await send('\r');await send('next');
  await send('\x13');expect(saved).toBe(original.slice(0,16)+'X'+original.slice(16)+'\nnext');
  expect(saved).not.toContain('\x1b');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
},15000);
