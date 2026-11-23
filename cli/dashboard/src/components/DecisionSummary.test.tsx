import React from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {DecisionSummary,formatRecordedAnswer} from "./DecisionSummary.js";
import {LanguageContext} from "../ui/PageFrame.js";

test("structured multi-question replies preserve every recorded user value",()=>{
  const reply=JSON.stringify({answers:{first:"Original 中文",second:"More input\nNEXT LINE"}});
  expect(formatRecordedAnswer(reply)).toBe("Original 中文\nMore input\nNEXT LINE");
  expect(formatRecordedAnswer("Choose option 1: Original label")).toBe("Choose option 1: Original label");
  expect(formatRecordedAnswer(JSON.stringify({answers:{second:"SECOND",first:"FIRST"}}),
    {questions:[{node_id:"first"},{node_id:"second"}]} as any)).toBe("FIRST\nSECOND");
});

test("the narrow bilingual summary stays concise, read-only and separate from the full options",async()=>{
  const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:false});
  const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:false,columns:32,rows:12});stdout.on("data",()=>{});
  const decision:any={task_id:"t",process_id:"a",decision_id:"q",question:"检查哪个文件？\n"+"问题".repeat(80),
    options:[{label:"FULL OPTION"}],why_user_must_decide:"FULL REASON",allow_free_form:true};
  const root=renderSync(<LanguageContext.Provider value="zh"><Box width={32}><DecisionSummary decision={decision} answer={"读取 README.md\n"+"更多输入".repeat(80)}/></Box></LanguageContext.Provider>,
    {stdin,stdout,stderr:stdout,patchConsole:false,exitOnCtrlC:false});
  const text=(node:any):string=>node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
  const nodes=(node:any):any[]=>[node,...(node.childNodes ?? []).flatMap(nodes)];
  try{
    await new Promise(resolve=>setTimeout(resolve,60));
    const ink:any=instances.get(stdout),screen=ink.frontFrame.screen;
    const frame=Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char??" ").join(""));
    expect(frame.filter(line=>line.trim()).length).toBe(3); // one summary line and the shared border
    expect(frame.join("\n")).toContain("检查");expect(frame.join("\n")).toContain("›");expect(frame.join("\n")).toContain("读取");
    expect(text(ink.rootNode)).not.toMatch(/FULL OPTION|FULL REASON|已答复/);
    expect(nodes(ink.rootNode).some(node=>node._eventHandlers?.onClick)).toBe(false);
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();}
});

test("display unwraps only a recorded keyboard choice matching its original option",()=>{
  const decision:any={options:[{label:"Gitgo 软件"},{label:"gitgo_test 文件夹"}]};
  expect(formatRecordedAnswer("Choose option 1: Gitgo 软件",decision)).toBe("Gitgo 软件");
  expect(formatRecordedAnswer("Choose option 2: gitgo_test 文件夹; conditions: 保留所有原文件",decision))
    .toBe("gitgo_test 文件夹; conditions: 保留所有原文件");
  for(const answer of ["用户直接输入的答复","Choose option 9: 未知","Choose option 1: 与原选项不符","Discuss option 1: Gitgo 软件; concern: 保留记录"])
    expect(formatRecordedAnswer(answer,decision)).toBe(answer);
  expect(formatRecordedAnswer("Choose option 1: Allow once",{options:[{label:"Allow once"}]} as any,
    {options:[{label:"仅允许一次"}]} as any)).toBe("仅允许一次");
});

test("each structured answer resolves against the matching original question without losing free input",()=>{
  const decision:any={questions:[{node_id:"first",options:[{label:"FIRST CHOICE"}]},{node_id:"second",options:[{label:"SECOND CHOICE"}]}]};
  const answer=JSON.stringify({answers:{second:"Choose option 1: SECOND CHOICE",first:"自由输入\n完整保留"}});
  expect(formatRecordedAnswer(answer,decision)).toBe("自由输入\n完整保留\nSECOND CHOICE");
  expect(answer).toContain("Choose option 1:");
});
