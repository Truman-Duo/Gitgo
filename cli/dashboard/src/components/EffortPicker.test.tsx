import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {EffortPicker} from "./EffortPicker.js";
import {DiscreteScale} from "./DiscreteScale.js";

function fixture(options:any={}){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{columns:options.width || 90,rows:18});let output="";stdout.on("data",(b:Buffer)=>output+=b);
 const stderr:any=new PassThrough();stderr.on("data",()=>{});const calls:any[]=[],footers:any[]=[];let dismissed=0;
 const client:any={ready:true,close(){},async callTool(op:string,args:any){calls.push([op,args]);if(op==='provider.effort.options')return {provider_id:'a',provider_name:'API A',model_id:'one',revision:'r',effort:'low',levels:['low','medium','high'],state:'declared',...options.data};if(options.fail)throw Error('SYNTHETIC_SWITCH_FAILURE');return {status:'selected'};}};
 const root=renderSync(<InputProvider><EffortPicker client={client} onFooter={footer=>footers.push(footer)} onDismiss={()=>dismissed++}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 return {calls,footers,output:()=>output,dismissed:()=>dismissed,send:async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,75));},close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("effort is a horizontal bounded scale, uses the current API/model revision and hides the input box",async()=>{
 const ui=fixture();try{await ui.send('');expect(ui.output()).toContain('Faster');expect(ui.output()).toContain('Deeper');expect(ui.output()).not.toContain('default');expect(ui.footers.at(-1)).toBe(null);await ui.send('\x1b[C');await ui.send('\r');expect(ui.calls[1]).toEqual(['provider.effort.select',{provider_id:'a',model_id:'one',effort:'medium',expected_revision:'r'}]);expect(ui.dismissed()).toBe(1);}finally{ui.close();}
});
test("effort cancellation does not write; service failures retain the staged selection",async()=>{
 const ui=fixture({fail:true});try{await ui.send('');await ui.send('\x1b[C');await ui.send('\r');expect(ui.output()).toContain('SYNTHETIC_SWITCH_FAILURE');expect(ui.dismissed()).toBe(0);await ui.send('\x1b');expect(ui.dismissed()).toBe(1);expect(ui.calls.filter(c=>c[0]==='provider.effort.select')).toHaveLength(1);}finally{ui.close();}
});
test("a declared default points at its actual level without adding a default option",async()=>{
 const ui=fixture({data:{effort:'',default_effort:'medium',effective_effort:'medium'}});
 try{await ui.send('');expect(ui.output()).not.toContain('default');await ui.send('\x1b[C');await ui.send('\r');expect(ui.calls[1][1].effort).toBe('high');}finally{ui.close();}
});
test("confirming a service default pins the explicit persisted value",async()=>{
 const ui=fixture({data:{effort:'',default_effort:'medium',effective_effort:'medium'}});
 try{await ui.send('');await ui.send('\r');expect(ui.calls[1][1].effort).toBe('medium');expect(ui.dismissed()).toBe(1);}finally{ui.close();}
});
test.each([
 {levels:['low','high','max'],expected:'high'},
 {levels:['low','medium'],expected:'medium'},
 {levels:['minimal','max'],expected:'minimal'},
])("an undeclared default stages a supported level and Enter really persists it: $expected",async({levels,expected})=>{
 const ui=fixture({data:{effort:'',levels,state:'reference'}});
 try{await ui.send('');expect(ui.output()).toContain('▲');expect(ui.output()).not.toContain('API A');expect(ui.output()).not.toContain('default');expect(ui.output()).not.toContain('metadata');expect(ui.calls).toHaveLength(1);
  await ui.send('\r');expect(ui.calls[1]).toEqual(['provider.effort.select',{provider_id:'a',model_id:'one',effort:expected,expected_revision:'r'}]);expect(ui.dismissed()).toBe(1);
 }finally{ui.close();}
});
test("cancelling a staged fallback does not commit it",async()=>{
 const ui=fixture({data:{effort:'',levels:['low','high']}});try{await ui.send('');await ui.send('\x1b');expect(ui.calls).toHaveLength(1);expect(ui.dismissed()).toBe(1);}finally{ui.close();}
});
test("unknown model capabilities do not become five invented effort levels",async()=>{
 const ui=fixture({width:26,data:{effort:'',levels:[],state:'unknown'}});try{await ui.send('');expect(ui.output()).toContain('No effort levels');expect(ui.output()).not.toContain('xhigh');await ui.send('\x1b[C');await ui.send('\r');expect(ui.calls).toHaveLength(1);}finally{ui.close();}
});
test("short scale windows retain the selected level and stay within physical width",async()=>{
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});const stdout:any=new PassThrough();Object.assign(stdout,{columns:9,rows:8});let output='';stdout.on('data',(b:Buffer)=>output+=b);const stderr:any=new PassThrough();stderr.on('data',()=>{});
 const root=renderSync(<DiscreteScale width={9} value="xhigh" items={['low','medium','high','xhigh','max'].map(id=>({id,label:id}))}/>,{stdin,stdout,stderr,patchConsole:false});
 try{await new Promise(r=>setTimeout(r,60));expect(output).toContain('xhigh');expect(output).not.toContain('medium');}finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
