import React,{useCallback} from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {copy} from "../ui/pageContract.js";
import {applicationBindings} from "../input/router.js";
import {menuHelpGuide} from "../ui/menuHelp.js";
import {renderOverlay} from "./overlays.js";
import {SuggestionRow} from "./Selection.js";
import {colors} from "../theme/tokens.js";
import chalk from "chalk";
import {focusedHeader} from "../testing/selectedRows.js";

function mount(type:string){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:112,rows:32});stdout.on('data',()=>{});
 const stderr:any=new PassThrough();stderr.on('data',()=>{});const calls:string[]=[];let dismissed=0,footer:any;
 const client:any={ready:true,close(){},async callTool(op:string){calls.push(op);if(op==='config.get')return {language:'en'};if(op==='governance.quality')return {suggestion_count:0,by_type:{}};return {};}};
 function Fixture(){const input=useTextInput(),onFooter=useCallback((f:any)=>{footer=f;},[]);
  const ctx:any={client,scene:'workspace',activeProject:'demo',w:112,h:32,llmCmdInput:input,cmdInput:input,dispatch(){},popOverlay(){dismissed++;},refresh(){},navigate(){},setFooterOverride:onFooter,setScreenStatusText(){},exit(){dismissed++;},projects:[],runCommandEffect(){},runCommandDeps:{},toolEvents:[],processes:{},sendChat(){},verbose:false,language:'en'};
  return <LanguageContext.Provider value="en"><Box width={112} height={32} flexDirection="column">{renderOverlay({type,props:{project:'demo'}},ctx)}</Box></LanguageContext.Provider>;
 }
 const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(stdout);
 return {calls,footer:()=>footer,dismissed:()=>dismissed,headerFocused(){const s=ink.frontFrame.screen;for(let y=0;y<s.height;y++){const text=Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||" ").join("");if((text.includes("General")&&text.includes("Providers"))||(text.includes("Daily activity")&&text.includes("Recent tasks"))||(text.includes("lesson")&&text.includes("contract")))return focusedHeader(ink,y);}return false;},async send(text:string){stdin.write(text);await new Promise(r=>setTimeout(r,110));},text(){const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join('')).join('\n');},close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
for(const type of ['configPanel','createForm','exportPanel','statsOverview','statsPanel','runtimeMenu','runtimeContext','governancePanel','memoryPanel','trialPanel','formalPanel','lessonsPanel','runtimeToolsPanel','context','binPanel','quitConfirm','undoPanel','compactPanel','btwPanel']){
 test(`${type}: scoped help preserves the menu and performs no backend operation`,async()=>{
  const ui=mount(type);try{await ui.send('');const original=ui.text(),reads=ui.calls.length;
   await ui.send('/help');await ui.send('\r');expect(ui.text()).toContain('Usage guide');expect(ui.text()).toContain('Key reference');
   expect(ui.calls).toHaveLength(reads);expect(ui.dismissed()).toBe(0);await ui.send('\x1b');
   expect(ui.text()).toBe(original);expect(ui.dismissed()).toBe(0);expect(ui.calls).toHaveLength(reads);
  }finally{ui.close();}
 });
}

test('create help retains staged input and /help inside field editing remains literal',async()=>{
 const ui=mount('createForm');try{await ui.send('');await ui.send('\r');await ui.send('draft-name');await ui.send('\r');
  await ui.send('/help');await ui.send('\r');expect(ui.text()).toContain('Configure a project workspace');await ui.send('\x1b');expect(ui.text()).toContain('draft-name');
  await ui.send('\r');await ui.send('/help');expect(ui.footer().kind).toBe('normal');expect(ui.footer().cmdInput.value).toBe('draft-name/help');expect(ui.text()).not.toContain('Usage guide');expect(ui.calls).not.toContain('project.create');
 }finally{ui.close();}
});

test('menu guidance follows actual save bindings and all topics are bilingual',()=>{
 const before=applicationBindings.snapshot();try{
  applicationBindings.configure({version:1,overrides:{'form.save':{key:'s',meta:true}}});
  expect(menuHelpGuide('create',v=>copy(v,'en'))!.steps.join(' ')).toContain('Alt+S');
  for(const topic of ['config','keybindings','create','export','usage','trace','update','runtime','context','governance','memory','tools','mechanisms','lessons','trial','formal','btw','quit','undo','compact','bin'])for(const lang of ['zh','en'] as const){const guide=menuHelpGuide(topic,v=>copy(v,lang))!;expect(guide.title.length).toBeGreaterThan(1);expect(guide.steps.length).toBeGreaterThan(0);}
 }finally{applicationBindings.configure(before);}
});

test('command recommendations retain a subdued tint of the command green; model choices stay neutral',async()=>{
 expect(colors.suggestion.active.fg).toBe(colors.input.command.border);expect(colors.suggestion.active.bg).not.toBe(colors.input.command.border);
 const previous=chalk.level;chalk.level=3;const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:60,rows:4});stdout.on('data',()=>{});const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const root=renderSync(<Box flexDirection="column"><SuggestionRow selected label="/help" description="Help"/><SuggestionRow selected tone="neutral" label="model"/></Box>,{stdin,stdout,stderr,patchConsole:false});
 try{await new Promise(r=>setTimeout(r,65));const ink:any=instances.get(stdout),style=(x:number,y:number)=>ink.stylePool.get(cellAt(ink.frontFrame.screen,x,y)!.styleId).map((s:any)=>s.code).join('');
  expect(style(1,0)).toContain('48;2;18;50;32');expect(style(1,0)).toContain('38;2;32;139;78');expect(style(1,0)).not.toContain('48;2;32;139;78');expect(style(1,1)).toContain('48;2;42;42;42');expect(style(9,0)).not.toContain('48;2;18;50;32');
 }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();chalk.level=previous;}
});

for(const type of ['configPanel','statsOverview','runtimeMenu'])test(`${type}: Down and Tab cannot enter header content; only Enter can`,async()=>{
 const previous=chalk.level;chalk.level=3;const ui=mount(type);try{await ui.send('');expect(ui.headerFocused()).toBe(true);
  await ui.send('\x1b[B');await ui.send('\t');expect(ui.headerFocused()).toBe(true);await ui.send('\r');expect(ui.headerFocused()).toBe(false);
  await ui.send('\x1b');expect(ui.dismissed()).toBe(0);expect(ui.headerFocused()).toBe(true);
 }finally{ui.close();chalk.level=previous;}
});
