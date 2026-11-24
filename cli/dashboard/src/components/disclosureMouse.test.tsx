import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {VirtualConversation} from "./VirtualConversation.js";

test("real SGR repeated clicks toggle the shared completed round on every release",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:100,rows:24});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const root=renderSync(<InputProvider><LanguageContext.Provider value="en"><Box width={100} height={24} flexDirection="column">
  <VirtualConversation contentWidth={98} scrollRef={React.createRef()} streaming={null} messages={[
   {message_id:"u",turn_id:"t",role:"user",content:"original request",timestamp:"now"},
   {message_id:"outcome:t",turn_id:"t",role:"assistant",kind:"outcome",status:"completed",content:"final answer",timestamp:"now",
    activity:[{kind:"progress",text:"original progress",visibility:"public"}]},
  ]}/>
 </Box></LanguageContext.Provider></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const wait=()=>new Promise(r=>setTimeout(r,45));
 const ink:any=instances.get(stdout);
 const frame=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||" ").join(""));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await wait();
  const row=frame().findIndex(line=>line.includes("Completed"));expect(row).toBeGreaterThanOrEqual(0);
  const col=frame()[row]!.indexOf("▸");expect(col).toBeGreaterThanOrEqual(0);
  const pulses:number[]=[];let last=performance.now();const pulse=setInterval(()=>{const now=performance.now();pulses.push(now-last);last=now;},10);
  try{for(let i=0;i<4;i++){
   stdin.write(`\x1b[<0;${col+1};${row+1}M\x1b[<0;${col+1};${row+1}m`);await wait();
   expect(frame()[row]).toContain(i%2?"▸":"▾");
  }
   const click=()=>'\x1b[<0;'+(col+1)+';'+(row+1)+'M\x1b[<0;'+(col+1)+';'+(row+1)+'m';
   stdin.write(click()+click());await wait();
   expect(frame()[row]).toContain("▸");
   stdin.write(click()+click()+click());await wait();
   expect(frame()[row]).toContain("▾");
   // Real terminal tremor, including release back on the triangle, stays an
   // action. The same gesture must close and then reopen the shared round.
   const jitter='\x1b[<0;'+(col+1)+';'+(row+1)+'M'
    +'\x1b[<32;'+(col+2)+';'+(row+1)+'M'
    +'\x1b[<32;'+(col+1)+';'+(row+1)+'M'
    +'\x1b[<0;'+(col+1)+';'+(row+1)+'m';
   stdin.write(jitter);await wait();expect(frame()[row]).toContain("▸");
   stdin.write(jitter);await wait();expect(frame()[row]).toContain("▾");
   // A deliberate drag out of the action is cancelled, even on return.
   stdin.write('\x1b[<0;'+(col+1)+';'+(row+1)+'M'
    +'\x1b[<32;'+(col+5)+';'+(row+1)+'M'
    +'\x1b[<0;'+(col+1)+';'+(row+1)+'m');
   await wait();expect(frame()[row]).toContain("▾");
  }finally{clearInterval(pulse);}
  expect(pulses.length).toBeGreaterThan(3);expect(Math.max(...pulses)).toBeLessThan(250);
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
