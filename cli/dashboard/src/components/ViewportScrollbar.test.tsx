import React,{createRef} from 'react';
import {expect,test} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box,Text,ScrollBox,renderSync,useTerminalSize,type ScrollBoxHandle} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {ViewportScrollbar,scrollbarGeometry,SCROLLBAR_HIDE_DELAY_MS} from './ViewportScrollbar.js';
import {InputProvider} from '../input/runtime.js';
const pause=()=>new Promise(resolve=>setTimeout(resolve,60));

test('one-cell scrollbar follows the existing viewport through wheel, track seeks, lazy growth and resize',async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:60,rows:12});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const scroll=createRef<ScrollBoxHandle>();let renders=0;
 function Fixture({count}:{count:number}){const {rows}=useTerminalSize();renders++;return <InputProvider><Box height={rows||12} width='100%'>
  <ScrollBox ref={scroll} flexGrow={1} minWidth={0} flexDirection='column'><Text>{Array.from({length:count},(_,i)=>`ROW ${i}`).join('\n')}</Text></ScrollBox>
  <ViewportScrollbar scrollRef={scroll}/></Box></InputProvider>;}
 const root=renderSync(<Fixture count={100}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
 const bar=()=>{const screen=ink.frontFrame.screen;return Array.from({length:screen.height},(_,y)=>cellAt(screen,screen.width-1,y)?.char||' ');};
 const click=(row:number)=>stdin.write(`\x1b[<0;${stdout.columns};${row+1}M\x1b[<0;${stdout.columns};${row+1}m`);
 try{
  await pause();stdin.write(`\x1b[<35;${stdout.columns};1M`);await pause();expect(bar()[0]).toBe('┃');click(11);await pause();expect(scroll.current!.isSticky()).toBe(true);expect(bar()[11]).toBe('┃');
  click(0);await pause();expect(scroll.current!.getScrollTop()).toBe(0);expect(bar()[0]).toBe('┃');
  scroll.current!.scrollBy(45);await pause();expect(bar().indexOf('┃')).toBeGreaterThan(0);expect(bar().indexOf('┃')).toBeLessThan(11);
  const committed=renders;scroll.current!.scrollBy(1);await pause();expect(renders).toBe(committed); // scrollbar updates don't rerender its reader
  scroll.current!.scrollToBottom();await pause();root.rerender(<Fixture count={250}/>);await pause();expect(bar()[11]).toBe('┃');
  stdout.rows=8;stdout.columns=35;stdout.emit('resize');await pause();expect(bar().length).toBe(8);expect(bar()[7]).toBe('┃');
  root.rerender(<Fixture count={2}/>);await pause();expect(bar().every(char=>char==='┃')).toBe(true);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

function reader(){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:60,rows:20});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const scroll=createRef<ScrollBoxHandle>();
 const root=renderSync(<InputProvider><Box width={60} height={20} paddingTop={2}>
  <ViewportScrollbar scrollRef={scroll}><ScrollBox ref={scroll} flexGrow={1} minWidth={0} flexDirection='column'>
   <Text>{Array.from({length:160},(_,i)=>`ROW ${i}`).join('\n')}</Text>
  </ScrollBox></ViewportScrollbar>
 </Box></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
 return {scroll,ink,mouse:(code:number,x:number,y:number,release=false)=>stdin.write(`\x1b[<${code};${x+1};${y+1}${release?'m':'M'}`),
  bar:()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>cellAt(s,59,y)?.char||' ');},
  close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}

test('thumb drag keeps its grab offset, captures outside the gutter and stops at release',async()=>{
 const ui=reader();
 try{await pause();const top=ui.scroll.current!.getViewportTop();
  ui.mouse(0,59,top);await pause();expect(ui.scroll.current!.getScrollTop()).toBe(0);
  ui.mouse(32,40,top+9);await pause();expect(ui.scroll.current!.getRenderedScrollTop()).toBeGreaterThan(0);
  ui.mouse(32,40,top+40);await pause();expect(ui.scroll.current!.isSticky()).toBe(true);
  ui.mouse(0,40,top+40,true);await pause();const after=ui.scroll.current!.getScrollTop();
  ui.mouse(32,59,top);await pause();expect(ui.scroll.current!.getScrollTop()).toBe(after);
 }finally{ui.close();}
});

test('scrollbar uses the painted position when a virtual clamp lags a seek target',async()=>{
 const ui=reader();
 try{await pause();ui.mouse(35,59,6);ui.scroll.current!.setClampBounds(0,20);ui.scroll.current!.scrollTo(110);await pause();
  expect(ui.scroll.current!.getScrollTop()).toBe(110);expect(ui.scroll.current!.getRenderedScrollTop()).toBe(20);
  const geometry=scrollbarGeometry(20,ui.scroll.current!.getScrollHeight(),ui.scroll.current!.getViewportHeight());
  expect(ui.bar().indexOf('┃')).toBe(ui.scroll.current!.getViewportTop()+geometry.start);
 }finally{ui.close();}
});

test('only the right gutter reveals the scrollbar; returning to the body hides after three seconds without reflow',async()=>{
 const ui=reader();
 try{await pause();const height=ui.scroll.current!.getScrollHeight(),top=ui.scroll.current!.getScrollTop();
  expect(ui.bar()).not.toContain('┃');
  ui.mouse(35,10,6);await pause();expect(ui.bar()).not.toContain('┃');
  ui.scroll.current!.scrollBy(5);await pause();expect(ui.bar()).not.toContain('┃');
  ui.scroll.current!.scrollTo(top);await pause();
  ui.mouse(35,59,6);await pause();expect(ui.bar()).toContain('┃');
  ui.mouse(35,10,6);await pause();expect(ui.bar()).toContain('┃');
  await new Promise(r=>setTimeout(r,SCROLLBAR_HIDE_DELAY_MS+60));expect(ui.bar()).not.toContain('┃');
  expect(ui.scroll.current!.getScrollHeight()).toBe(height);expect(ui.scroll.current!.getScrollTop()).toBe(top);
  ui.mouse(0,59,ui.scroll.current!.getViewportTop());ui.mouse(32,59,19);await pause();
  expect(ui.bar()).toContain('┃');expect(ui.scroll.current!.getScrollTop()).toBeGreaterThan(0);
  ui.mouse(0,59,19,true);
 }finally{ui.close();}
},10000);
