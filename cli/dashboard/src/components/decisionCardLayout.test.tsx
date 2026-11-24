import React,{createRef} from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync,type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {VirtualConversation} from "./VirtualConversation.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import type {ChatMessage,PendingDecision} from "../types.js";

const pause=(ms=70)=>new Promise(resolve=>setTimeout(resolve,ms));
const text=(node:any):string=>node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
const nodes=(node:any):any[]=>[node,...(node.childNodes ?? []).flatMap(nodes)];
function bounds(node:any){
  let x=0,y=0;
  for(let part=node;part;part=part.parentNode){x+=part.yogaNode?.getComputedLeft() || 0;y+=part.yogaNode?.getComputedTop() || 0;}
  return {x,y,width:node.yogaNode?.getComputedWidth() || 0,height:node.yogaNode?.getComputedHeight() || 0};
}
const question="你想问的是 Gitgo 软件界面或功能的变化，还是桌面上 gitgo_test 文件夹的变化？";
const decision:PendingDecision={task_id:"turn",process_id:"A",decision_id:"question",kind:"clarification",question,
  why_user_must_decide:"REASON 原因",options:[{label:"Gitgo 软件",description:"软件界面及功能",principle:"PRINCIPLE",
    immediate_effect:"EFFECT",downstream_effect:"DOWNSTREAM",risks:"RISK",reversibility:"REVERSIBILITY",recommended:false},
    {label:"gitgo_test 文件夹",description:"文件夹内容",principle:"PRINCIPLE",immediate_effect:"EFFECT",downstream_effect:"DOWNSTREAM",risks:"RISK",reversibility:"REVERSIBILITY",recommended:false}],allow_free_form:true};

function mount(messages:ChatMessage[],language:"en"|"zh",initialWidth=144){
  let columns=initialWidth;
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns,rows:64});stdout.on("data",()=>{});
  const stderr:any=new PassThrough();stderr.on("data",()=>{});
  const scroll=createRef<ScrollBoxHandle>();
  const draw=()=><InputProvider><LanguageContext.Provider value={language}><Box width={columns} height={64} paddingX={1}>
    <VirtualConversation messages={messages} streaming={null} contentWidth={columns-2} scrollRef={scroll}/>
  </Box></LanguageContext.Provider></InputProvider>;
  const root=renderSync(draw(),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const tree=()=>(instances.get(stdout) as any).rootNode;
  const frame=()=>{const screen=(instances.get(stdout) as any).frontFrame.screen;return Array.from({length:screen.height},(_,y)=>
    Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char ?? " ").join(""));};
  return {tree,frame,scroll,input:(value:string)=>stdin.write(value),
    focusDetail:(node:any)=>{const viewport=nodes(node).find(item=>item.style?.overflowY==='scroll');expect(viewport).toBeDefined();
      viewport.parentNode._eventHandlers.onMouseEnter({});return viewport;},
    width:()=>(instances.get(stdout) as any).frontFrame.screen.width,
    cell:(x:number,y:number)=>cellAt((instances.get(stdout) as any).frontFrame.screen,x,y)?.char,
    click(label:string){const target=nodes(tree()).find(node=>node._eventHandlers?.onClick && text(node).includes(label));
      expect(target).toBeDefined();target._eventHandlers.onClick({});},
    resize(value:number){columns=value;stdout.columns=value;root.rerender(draw());},
    cards:()=>nodes(tree()).filter(node=>node.style?.borderStyle==="single" && node.style?.borderRight!==false),
    close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}

for(const language of ["en","zh"] as const)test(`completed question summaries and full card remain inside nested terminal bounds (${language})`,async()=>{
  const recorded="Choose option 1: Gitgo 软件";
  const messages:ChatMessage[]=[{role:"user",content:"检查变化",turn_id:"turn",timestamp:"now",message_id:"user"},
    {role:"assistant",content:question,turn_id:"turn",timestamp:"now",message_id:"decision",decision,decision_answer:recorded},
    {role:"assistant",content:"FINAL ANSWER",turn_id:"turn",timestamp:"now",message_id:"final",status:"completed"}];
  const io=mount(messages,language);
  const check=()=>{
    expect(io.cards().length).toBeGreaterThan(0);
    for(const node of io.cards()){
      const box=bounds(node),limit=io.width();
      if(box.x+box.width>limit-1 && process.env.GITGO_CARD_LAYOUT_DEBUG){
        const ancestors=[];for(let part=node;part;part=part.parentNode)if(part.yogaNode)ancestors.push({bounds:bounds(part),style:part.style});
        console.log(JSON.stringify(ancestors));
      }
      expect(box.x+box.width).toBeLessThanOrEqual(limit-1);
      if(box.y>=0 && box.y+box.height<64){
        expect(io.cell(Math.round(box.x+box.width-1),Math.round(box.y))).toBe("┐");
        expect(io.cell(Math.round(box.x+box.width-1),Math.round(box.y+box.height-1))).toBe("┘");
      }
    }
  };
  try{
    await pause();check();
    expect(io.frame().filter(line=>line.includes("›"))).toHaveLength(1);
    expect(io.frame().find(line=>line.includes("›"))).toContain("Gitgo 软件");
    expect(io.frame().join("\n")).not.toContain("Choose option");
    expect(io.cards()[0]!.yogaNode.getComputedHeight()).toBe(3);
    io.click(language==="zh"?"已完成":"Completed");await pause(250);check();
    expect(io.cards()).toHaveLength(2);
    const inner=io.cards()[1]!;
    expect(inner.yogaNode.getComputedHeight()).toBe(3);
    expect(text(inner)).toContain(question);expect(text(inner)).toContain("› Gitgo 软件");
    for(const width of [34,80,144]){io.resize(width);await pause(100);check();
      for(const line of io.frame().filter(line=>line.includes("›")))expect(line).toContain("Gitgo 软件");
      for(const card of io.cards()){
        if(card.yogaNode.getComputedHeight()!==3 && process.env.GITGO_CARD_LAYOUT_DEBUG)console.log(JSON.stringify({width,frame:io.frame().slice(0,19),
          children:nodes(card).filter(node=>node.yogaNode).map(node=>({text:text(node).slice(0,100),bounds:bounds(node),style:node.style}))}));
        expect(card.yogaNode.getComputedHeight()).toBe(3);
      }}
    const header=nodes(inner).find(node=>node._eventHandlers?.onClick);
    expect(header).toBeDefined();header._eventHandlers.onClick({});await pause(250);check();
    expect(text(inner)).toContain("REASON 原因");expect(text(inner)).toContain("gitgo_test 文件夹");
    expect(text(inner).split(question)).toHaveLength(3); // Compact header plus complete reading body.
    for(const field of ["PRINCIPLE","EFFECT","DOWNSTREAM","RISK","REVERSIBILITY"])expect(text(inner)).toContain(field);
    for(const width of [80,34,144]){io.resize(width);await pause(220);check();expect(text(io.tree())).toContain("REASON 原因");}
  }finally{io.close();}
});

test("long free replies and option details stay fully readable while parent updates continue",async()=>{
  const answer=("原始输入 中文 e\u0301 👨‍👩‍👧‍👦\n".repeat(210))+"FINAL USER INPUT";
  const description=Array.from({length:120},(_,index)=>`原始选项详情 ${index}`).join("\n")+"\nFINAL OPTION DETAIL";
  const original={...decision,options:decision.options.map((option,index)=>({...option,description:index ? description : option.description}))};
  const messages:ChatMessage[]=[{role:"user",content:"检查变化",turn_id:"turn",timestamp:"now",message_id:"user"},
    {role:"assistant",content:question,turn_id:"turn",timestamp:"now",message_id:"decision",decision:original,decision_answer:answer},
    {role:"assistant",content:"FINAL ANSWER",turn_id:"turn",timestamp:"now",message_id:"final",status:"completed"}];
  const io=mount(messages,"zh",80),gaps:number[]=[];let pulse:ReturnType<typeof setInterval>|undefined,updates=0;
  try{
    await pause();io.click("已完成");await pause(230);
    const inner=io.cards()[1]!,header=nodes(inner).find(node=>node._eventHandlers?.onClick);
    let last=performance.now();pulse=setInterval(()=>{const now=performance.now();gaps.push(now-last);last=now;updates++;io.resize(80);},10);
    // Observe the expansion animation and a steady-state interval afterwards.
    header._eventHandlers.onClick({});await pause(650);
    clearInterval(pulse);pulse=undefined;
    if(process.env.GITGO_CARD_LAYOUT_DEBUG)console.log(JSON.stringify({updates,gaps}));
    expect(updates).toBeGreaterThan(5);expect(Math.max(...gaps)).toBeLessThan(250);
    const p95=gaps.slice().sort((a,b)=>a-b)[Math.floor(gaps.length*.95)]!;
    expect(p95).toBeLessThan(100);
    expect(nodes(io.tree()).length).toBeLessThan(500);
    expect(text(inner)).toContain(answer);expect(text(inner)).toContain(description);
    const fullReply=nodes(inner).find(node=>node.yogaNode && text(node)==="› "+answer && bounds(node).height>1)!;
    expect(fullReply).toBeDefined();
    const viewport=io.focusDetail(inner),height=bounds(viewport).height;
    const target=Math.max(0,Math.floor(bounds(fullReply).y+bounds(fullReply).height-bounds(viewport).y-height));
    const page=Math.max(1,height-1);
    io.input('\x1b[H'+'\x1b[6~'.repeat(Math.floor(target/page))+'\x1b[B'.repeat(target%page));await pause(230);
    for(let attempt=0;attempt<60 && viewport.pendingScrollDelta;attempt++)await pause(20);
    expect(viewport.pendingScrollDelta||0).toBe(0);
    if(process.env.GITGO_CARD_LAYOUT_DEBUG)console.log(JSON.stringify({target,page,reply:bounds(fullReply),viewport:bounds(viewport),
      scrollTop:viewport.scrollTop,pending:viewport.pendingScrollDelta}));
    expect(io.frame().join("\n")).toContain("FINAL USER INPUT");
    io.input('\x1b[F');await pause(230);
    expect(io.frame().join("\n")).toContain("FINAL OPTION DETAIL");
    if(process.env.GITGO_DECISION_PERF_REPORT)await Bun.write(process.env.GITGO_DECISION_PERF_REPORT,JSON.stringify({
      evidence:"shared frontend component viewport and simulated parent rerenders; not formal terminal or LLM latency",
      reply_codepoints:[...answer].length,option_detail_lines:121,parent_updates:updates,
      ui_pulse_max_ms:Math.max(...gaps),ui_pulse_p95_ms:p95,dom_nodes:nodes(io.tree()).length,
      full_reply_and_option_detail_readable:true,model_requests:0},null,2));
  }finally{if(pulse)clearInterval(pulse);io.close();}
},10000);
