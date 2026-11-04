import React,{useRef} from "react";
import {test,expect} from "bun:test";
import {Box,Text,ScrollBox,renderSync,type ScrollBoxHandle} from "@anthropic/ink";
import {PassThrough} from "node:stream";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider,useScrollInput} from "../input/runtime.js";
import {FooterDrawer} from "./FooterDrawer.js";

for(const viewport of ['shared','self'] as const)test(`FooterDrawer ${viewport}: bounded viewport, owned wheel and resizing keep surrounding UI visible`,async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:64,rows:20});stdout.on('data',()=>{});const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const content=Array.from({length:50},(_,i)=><Text key={i}>row-{String(i).padStart(2,'0')}</Text>);
 function Owned(){const ref=useRef<ScrollBoxHandle>(null);useScrollInput(ref);return <ScrollBox ref={ref} flexDirection="column" flexGrow={1} minHeight={0}>{content}</ScrollBox>;}
 function Scene({height}:{height:number}){return <InputProvider><Box height={height} width={64} flexDirection="column"><Box flexGrow={1}><Text>BODY</Text></Box><Text>EDITOR</Text><FooterDrawer terminalRows={height} viewport={viewport}>{viewport==='self'?<Owned/>:content}</FooterDrawer><Text>STATUS</Text></Box></InputProvider>;}
 const root=renderSync(<Scene height={20}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(stdout);
 const lines=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char??' ').join(''));};
 const send=async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,90));};
 try{ink.setAltScreenActive(true);ink.onRender();await send('');
  expect(lines().filter(line=>line.includes('row-'))).toHaveLength(10);expect(lines().at(-1)).toContain('STATUS');expect(lines().join('\n')).toContain('BODY');expect(lines().join('\n')).toContain('EDITOR');
  await send('\x1b[<65;5;15M');expect(lines().join('\n')).toContain('row-03');
  await send('\x1b[<65;5;15M'.repeat(50));expect(lines().join('\n')).toContain('row-49');
  await send('\x1b[<64;5;15M');expect(lines().join('\n')).toContain('row-37');
  stdout.rows=12;stdout.emit('resize');root.rerender(<Scene height={12}/>);await send('');
  expect(lines().filter(line=>line.includes('row-'))).toHaveLength(6);expect(lines().at(-1)).toContain('STATUS');expect(lines().join('\n')).toContain('EDITOR');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
