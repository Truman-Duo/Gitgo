import React,{createRef} from 'react';
import {expect,test} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box,renderSync,useTerminalSize,type ScrollBoxHandle} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {cellAt} from '../../vendor/ink/src/core/screen.js';
import {InputProvider} from '../input/runtime.js';
import {LanguageContext} from '../ui/PageFrame.js';
import type {ChatMessage} from '../types.js';
import {VirtualConversation} from './VirtualConversation.js';

const pause=(ms=60)=>new Promise(resolve=>setTimeout(resolve,ms));
const messages:ChatMessage[]=Array.from({length:300},(_,index)=>({role:'user',content:`OLD PROMPT ${index}`,message_id:`u${index}`,timestamp:'now'}));
const outcome:ChatMessage={role:'assistant',message_id:'result',turn_id:'latest',status:'completed',timestamp:'now',content:'FINAL ANSWER',
 activity:[{kind:'progress',text:'Saved public report',visibility:'public'},{kind:'tool',toolIndex:0}],
 tools:[{tool_call_id:'call1',process_id:'p',tool_name:'read_file',target:'source.txt',state:'completed',allowed:true,status_label:'OK',timestamp:'now',duration_ms:1,
 result_text:Array.from({length:100},(_,i)=>`SAVED FILE LINE ${i}`).join('\n')}]};
const history=[...messages,{role:'user',content:'LATEST PROMPT',message_id:'latest',turn_id:'latest',timestamp:'now'} as ChatMessage,outcome];

function fixture(initial:ChatMessage[]=history){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:90,rows:18});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const scroll=createRef<ScrollBoxHandle>();
 function Reader({items}:{items:ChatMessage[]}){const {columns,rows}=useTerminalSize();return <InputProvider><LanguageContext.Provider value='en'>
  <Box width='100%' height={rows||18} flexDirection='column'><VirtualConversation messages={items} streaming={null} contentWidth={(columns||90)-4} scrollRef={scroll}/></Box>
 </LanguageContext.Provider></InputProvider>;}
 const root=renderSync(<Reader items={initial}/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
 const lines=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join(''));};
 return {scroll,stdout,lines,ink,rerender:(items:ChatMessage[])=>root.rerender(<Reader items={items}/>),
  async click(label:string){const frame=lines(),row=frame.findIndex(line=>line.includes(label));expect(row).toBeGreaterThanOrEqual(0);
   const col=frame[row]!.search(/[▸▾]/);expect(col).toBeGreaterThanOrEqual(0);
   stdin.write(`\x1b[<0;${col+1};${row+1}M\x1b[<0;${col+1};${row+1}m`);await pause();return row;},
  close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}

test('asynchronously loaded history starts at latest; polling and resize respect a manual reading position',async()=>{
 const io=fixture([]);try{
  await pause();io.rerender(history);await pause(160);expect(io.lines().join('\n')).toContain('FINAL ANSWER');
  io.scroll.current!.scrollTo(0);await pause(160);expect(io.lines().join('\n')).toContain('OLD PROMPT 0');
  io.rerender([...history]);await pause();io.stdout.columns=55;io.stdout.rows=14;io.stdout.emit('resize');await pause(160);
  expect(io.scroll.current!.isSticky()).toBe(false);expect(io.lines().join('\n')).toContain('OLD PROMPT 0');
 }finally{io.close();}
});

for(const status of ['completed','failed'])test(`long ${status} reader retains choices through reflow, virtual eviction and repeated toggles`,async()=>{
 const io=fixture([...history.slice(0,-1),{...outcome,status}]);const label=status==='failed'?'Failed':'Completed';try{
  await pause(150);const headerRow=await io.click(label);
  expect(io.lines().findIndex(line=>line.includes(label))).toBeLessThanOrEqual(headerRow);
  io.scroll.current!.scrollBy(5);await pause();
  expect(io.lines().join('\n')).toContain('read_file');await io.click('read_file');
  expect(io.lines().join('\n')).toContain('SAVED FILE LINE 0');
  io.stdout.columns=55;io.stdout.rows=22;io.stdout.emit('resize');await pause(180);
  io.scroll.current!.scrollToBottom();await pause();
  expect(io.lines().join('\n')).toContain('SAVED FILE LINE 0');
  io.scroll.current!.scrollTo(0);await pause(120);expect(io.lines().join('\n')).toContain('OLD PROMPT 0');
  io.scroll.current!.scrollToBottom();await pause(180);
  expect(io.lines().join('\n')).toContain('SAVED FILE LINE');
  for(let index=0;index<6;index++){
   await io.click('read_file');io.scroll.current!.scrollToBottom();await pause();
   expect(io.lines().join('\n').includes('SAVED FILE LINE')).toBe(index%2===1);
  }
 }finally{io.close();}
});
