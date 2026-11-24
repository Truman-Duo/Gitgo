import React,{createRef} from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,Text,ScrollBox,renderSync,type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {AnimatedReveal} from "./AnimatedReveal.js";

const pause=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
test("already open asynchronous content grows to its natural height instead of re-entering a clipped reveal",async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:false});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:80,rows:30});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const draw=(height:number,expanded=true)=><Box flexDirection="column" width={80}>
    <AnimatedReveal expanded={expanded}><Box height={height}><Text>ASYNC CONTENT</Text></Box></AnimatedReveal></Box>;
  const root=renderSync(draw(2),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const height=()=>((instances.get(stdout) as any).rootNode.childNodes[0].childNodes[0].yogaNode.getComputedHeight());
  try{
    await pause(50);expect(height()).toBe(2);
    root.rerender(draw(200));await pause(40);expect(height()).toBe(200);
    root.rerender(draw(4));await pause(40);expect(height()).toBe(4);
    root.rerender(draw(4,false));await pause(260);expect(height()).toBe(0);
    root.rerender(draw(4));await pause(260);expect(height()).toBe(4);
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});

for(const live of [false,true])test(`lazy growth preserves reading position while explicit bottom following still works (live=${live})`,async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:false});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:60,rows:12});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const scroll=createRef<ScrollBoxHandle>();
  const draw=(lines:number)=><Box width={60} height={12}><ScrollBox ref={scroll} stickyScroll={live} flexGrow={1} flexDirection="column">
    <AnimatedReveal expanded><Text>{Array.from({length:lines},(_,index)=>`ORIGINAL LINE ${index}`).join("\n")}</Text></AnimatedReveal>
  </ScrollBox></Box>;
  const root=renderSync(draw(2),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const frame=()=>{const screen=(instances.get(stdout) as any).frontFrame.screen;return Array.from({length:screen.height},(_,y)=>
    Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char || " ").join("")).join("\n");};
  try{
    await pause(50);root.rerender(draw(60));await pause(60);
    if(live)expect(frame()).toContain("ORIGINAL LINE 59");
    else{expect(frame()).toContain("ORIGINAL LINE 0");expect(frame()).not.toContain("ORIGINAL LINE 59");}
    scroll.current!.scrollToBottom();await pause(50);expect(frame()).toContain("ORIGINAL LINE 59");
    root.rerender(draw(70));await pause(50);expect(frame()).toContain("ORIGINAL LINE 69");
    scroll.current!.scrollTo(0);await pause(50);root.rerender(draw(80));await pause(50);
    expect(frame()).toContain("ORIGINAL LINE 0");expect(frame()).not.toContain("ORIGINAL LINE 79");
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
