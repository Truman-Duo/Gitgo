import React from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync,Box} from "@anthropic/ink";
import {InputProvider} from "../../input/runtime.js";
import {useTextInput} from "../../hooks/useTextInput.js";
import {ProviderEditor} from "./ProviderEditor.js";
import {LanguageContext} from "../../ui/PageFrame.js";
import type {BackendClient} from "../../backend/client.js";
import {applicationBindings} from "../../input/router.js";

function mount(callTool:any,options:any={}){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{columns:options.width || 100,rows:options.height || 30});
 const stderr:any=new PassThrough();let output="";stdout.on("data",(b:Buffer)=>output+=b);stderr.on("data",()=>{});
 const footers:any[]=[],done:any[]=[],client:BackendClient={ready:true,close(){},callTool};
 function Fixture(){const input=useTextInput("");return <InputProvider><LanguageContext.Provider value={options.language || "en"}><Box width={options.width || 100} height={options.height || 30} flexDirection="column"><ProviderEditor client={client} project="fixture" cmdInput={input} onFooter={footer=>footers.push(footer)} seed={options.seed} onDone={(...args)=>done.push(args)}/></Box></LanguageContext.Provider></InputProvider>;}
 const root=renderSync(<Fixture/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 return {send:async(text:string)=>{stdin.write(text);await new Promise(r=>setTimeout(r,75));},output:()=>output,footers,done,close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
const seed={id:"p",name:"Existing",base_url:"https://api.test/v1",model_id:"old",context_window:64000,max_output_tokens:4096,protocol:"openai_chat",revision:"r1",api_key_present:true,configuration:{sources:{context_window:"user",max_output_tokens:"user"}}};
const listing={receipt_id:"models-current",models:[{id:"a",name:"a",limits:{context_window:32000,max_output_capacity:8000}},{id:"b",name:"b",limits:{context_window:64000,max_output_capacity:16000}}]};
const move=async(ui:any,index:number)=>{for(let i=0;i<12;i++)await ui.send("\x1b[A");for(let i=0;i<index;i++)await ui.send("\t");};
const paste=async(ui:any,text:string)=>ui.send('\x1b[200~'+text+'\x1b[201~');
const replaceDocument=async(ui:any,value:any)=>{await ui.send("\x1b[H");await ui.send("\x0b");await paste(ui,typeof value==='string'?value:JSON.stringify(value,null,2));};
const document=(entries:any[]=[{model_id:"old",context_window:64000,max_output_tokens:4096}])=>({name:"Edited",base_url:seed.base_url,preset_id:"custom",protocol:"openai_chat",models:entries.map(p=>({protocol:"openai_chat",request_preferences:{},...p}))});

test("new API Esc returns to its provider choice and choosing the same template preserves the complete draft",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models')return listing;return {};});
 try{
  await ui.send('');expect(ui.output()).not.toContain(seed.base_url);expect(ui.output()).not.toContain('1/1');await ui.send('OpenAI');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-key');await ui.send('\r');
  await ui.send('\x13');await ui.send('\r');await ui.send('\x13');
  await ui.send('\x1b');expect(ui.output()).toContain('Choose Provider');expect(ui.done).toHaveLength(0);
  await ui.send('OpenAI');await ui.send('\r');expect(ui.output()).toContain('Models (1)');await ui.send('\x13');
  expect(calls.find(c=>c[0]==='provider.save')[1]).toMatchObject({name:'OpenAI',api_key:'synthetic-key',model_id:'a'});
 }finally{ui.close();}
});

test("Esc from the initial provider choice exits; a choice opened inside an existing API returns to that API",async()=>{
 const initial=mount(async()=>({}));try{await initial.send('');await initial.send('\x1b');expect(initial.done).toEqual([[false]]);}finally{initial.close();}
 const edit=mount(async(op:string)=>op==='provider.models'?{models:[]}:{},{seed});
 try{await edit.send('');await edit.send('\r');await edit.send('\x1b');expect(edit.done).toHaveLength(0);await edit.send('\x1b');expect(edit.done).toEqual([[false]]);}finally{edit.close();}
});

test("configured models are checked automatically without selecting or pressing Enter on a model row",async()=>{
 const calls:any[]=[];const models=['one','two','three'].map(model_id=>({model_id,protocol:'openai_chat',context_window:32000,max_output_tokens:2048,request_preferences:{}}));
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return {models:[]};if(op==='provider.connection')return {checks:{model_response:'verified'}};return {};},{seed:{...seed,model_id:'one',configuration:{models}}});
 try{
  await new Promise(r=>setTimeout(r,2300));
  expect(calls.filter(c=>c[0]==='provider.connection').map(c=>c[1].draft.model_id)).toEqual(['one','two','three']);
  await ui.send('\r');await ui.send('\x1b');await ui.send('');
  expect(calls.filter(c=>c[0]==='provider.connection')).toHaveLength(3);
 }finally{ui.close();}
});

test("saving filled API details opens automatic model discovery without a missing-model operation error",async()=>{
 const calls:any[]=[];let resolveModels:any;
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models')return args.draft.api_key?new Promise(resolve=>resolveModels=resolve):{models:[],reference_only:true,credentials_required:true};return {};});
 try{
  await ui.send('');await ui.send('OpenAI');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\x13');
  expect(ui.output()).toContain('Add models');expect(ui.output()).toContain('Loading');
  expect(ui.output()).not.toContain('Operation failed');
  await ui.send('\x13');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  expect(calls.filter(c=>c[0]==='provider.models'&&c[1].draft.api_key)).toHaveLength(1);
  resolveModels(listing);await ui.send('');expect(ui.output()).toContain('[ ] a');
  expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\r');await ui.send('\x13');expect(ui.output()).toContain('Models (1)');
  await ui.send('\x13');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
  expect(calls.find(c=>c[0]==='provider.save')[1].models.map((p:any)=>p.model_id)).toEqual(['a']);
 }finally{ui.close();}
});

test("the save handoff exposes discovery failure and accepts manual models without a second navigation",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models'){if(args.draft.api_key)throw Error('MODEL_LIST_HTTP_401: Discovery unavailable');return {models:[],reference_only:true,credentials_required:true};}return {};});
 try{
  await ui.send('');await ui.send('OpenAI');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\x13');
  expect(ui.output()).toContain('Add models');expect(ui.output()).toContain('Model list authentication failed');expect(ui.output()).not.toContain('Operation failed');
  await ui.send('\r');await paste(ui,'manual-one\nmanual-two');await ui.send('\x13');
  expect(ui.output()).toContain('Models (2)');await ui.send('\x13');
  expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
  expect(calls.find(c=>c[0]==='provider.save')[1].models.map((p:any)=>p.model_id)).toEqual(['manual-one','manual-two']);
 }finally{ui.close();}
});

test("an empty discovery result explains manual entry and cannot silently finish an empty selection",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models')return {models:[]};return {};});
 try{
  await ui.send('');await ui.send('OpenAI');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\x13');
  expect(ui.output()).toContain('The API returned no models');expect(ui.output()).not.toContain('Operation failed');
  await ui.send('\x13');expect(ui.output()).toContain('Select a model or enter a model ID');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\r');await ui.send('manual-one');await ui.send('\x13');await ui.send('\x13');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
 }finally{ui.close();}
});

test("cancelling the pending save handoff cancels discovery without auto-retrying or losing staged credentials",async()=>{
 const calls:any[]=[];let resolveModels:any;
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models')return args.draft.api_key?new Promise(resolve=>resolveModels=resolve):{models:[]};return {};});
 try{
  await ui.send('');await ui.send('OpenAI');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\x13');
  const count=calls.filter(c=>c[0]==='provider.models').length;await ui.send('\x1b');
  expect(calls.some(c=>c[0]==='provider.cancel')).toBe(true);
  resolveModels(listing);await ui.send('');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(count);
  expect(ui.done).toHaveLength(0);expect(ui.output()).not.toContain('Operation failed');
  await ui.send('\r');await ui.send('\r');await ui.send('manual-after-cancel');await ui.send('\x13');await ui.send('\x13');
  expect(calls.find(c=>c[0]==='provider.save')[1]).toMatchObject({api_key:'synthetic-secret',model_id:'manual-after-cancel'});
 }finally{ui.close();}
});

test("a delayed model discovery failure cannot turn editing a connection field into a failed operation",async()=>{
 const calls:any[]=[];let rejectDiscovery:any;
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return new Promise((_,reject)=>rejectDiscovery=reject);return {};},{seed});
 try{
  await ui.send('');await ui.send('\t');await ui.send('\t');await ui.send('\r');
  rejectDiscovery(Error('MODEL_LIST_HTTP_401: Discovery/test unavailable. Draft retained'));
  await ui.send('');expect(ui.output()).not.toContain('Operation failed');
  expect(calls.some(c=>c[0]==='provider.cancel')).toBe(true);
  for(let i=0;i<seed.base_url.length;i++)await ui.send('\x7f');
  await ui.send('https://changed.test/v1');await ui.send('\r');
  expect(ui.output()).toContain('https://changed.test/v1');
  expect(ui.output()).not.toContain('Operation failed');
  expect(calls.filter(c=>c[0]==='provider.models').at(-1)[1].draft).toMatchObject({base_url:'https://changed.test/v1',api_key:'',retain_api_key:false});
 }finally{ui.close();}
});

test("a current model discovery rejection is a model-list notice, not a field or save failure",async()=>{
 let rejectDiscovery:any;
 const ui=mount(async(op:string)=>{if(op==='provider.models')return new Promise((_,reject)=>rejectDiscovery=reject);return {};},{seed});
 try{
  await ui.send('');rejectDiscovery(Error('MODEL_LIST_HTTP_401: Discovery/test unavailable. Draft retained'));await ui.send('');
  expect(ui.output()).not.toContain('Operation failed');
  expect(ui.output()).toContain('Model list authentication failed');
  await move(ui,6);await ui.send('\r');expect(ui.output()).toContain('Model list authentication failed');
  expect(ui.output()).toContain('Manual model IDs');
 }finally{ui.close();}
});

test("saving an unfinished new API stages the active key and focuses model addition without requiring a saved connection",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'openai',name:'OpenAI',base_url:seed.base_url,protocol:'openai_responses'}]};if(op==='provider.models')return listing;return {};});
 try{
  await ui.send('');await ui.send('OpenAI');await ui.send('\r');
  expect(calls.find(c=>c[0]==='provider.models')[1].draft.api_key).toBe('');
  await ui.send('\r');await ui.send('synthetic-secret');
  expect(ui.footers.at(-1).statusText).toBe('');
  await ui.send('\x13');
  expect(ui.output()).toContain('Add models');expect(ui.output()).not.toContain('Operation failed');
  expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  expect(calls.filter(c=>c[0]==='provider.models').at(-1)[1].draft.api_key).toBe('synthetic-secret');
  await ui.send('\r');await ui.send('\x13');await ui.send('\x13');
  expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
  expect(calls.find(c=>c[0]==='provider.save')[1]).toMatchObject({name:'OpenAI',api_key:'synthetic-secret',models:[{model_id:'a',protocol:'openai_responses'}]});
  expect(ui.output()).not.toContain('synthetic-secret');
 }finally{ui.close();}
});

test("manual models can be added before connection details and each missing input remains editable after save",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')throw Error('SYNTHETIC_CATALOG_UNAVAILABLE');return {};});
 try{
  await ui.send('');await ui.send('\r'); // Custom: no name, address, key or catalogue yet.
  await ui.send('\t');await ui.send('\t');await ui.send('\r');
  await ui.send('\r');await ui.send('manual-gpt-model');await ui.send('\x13');
  expect(ui.output()).toContain('Models (1)');expect(calls.some(c=>c[0]==='provider.models')).toBe(false);
  await ui.send('\x13');expect(ui.output()).toContain('Enter a connection name.');
  await ui.send('\r');await ui.send('My GPT API');await ui.send('\x13');
  expect(ui.output()).toContain('Enter the API address;');
  await ui.send('\r');await ui.send('https://api.test/v1');await ui.send('\x13');
  expect(ui.output()).toContain('Enter an API Key;');
  expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\x13');
  const saves=calls.filter(c=>c[0]==='provider.save');expect(saves).toHaveLength(1);
  expect(saves[0][1]).toMatchObject({name:'My GPT API',base_url:'https://api.test/v1',api_key:'synthetic-secret',model_id:'manual-gpt-model'});
  expect(ui.output()).not.toContain('synthetic-secret');
 }finally{ui.close();}
});

test("template enters the API form and discovers immediately; only confirmed batch candidates are saved",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'mimo',name:'MiMo',base_url:seed.base_url,protocol:'openai_chat'}]};if(op==='provider.models')return listing;return {};});
 try{
  await ui.send('');await ui.send('MiMo');await ui.send('\r');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(1);expect(calls.find(c=>c[0]==='provider.models')[1].draft.api_key).toBe('');
  expect(ui.output()).toContain('Models (0)');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\r');
  await move(ui,5);await ui.send('\r');await ui.send('\r');await ui.send('\x1b[B');await ui.send('\r');await ui.send('\x13');
  expect(ui.output()).toContain('Models (2)');expect(ui.footers.at(-1).hidden).toBe(true);expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\x13');const saved=calls.find(c=>c[0]==='provider.save')[1];expect(saved.models.map((p:any)=>p.model_id)).toEqual(['a','b']);expect(saved.model_id).toBe('a');
  expect(ui.output()).not.toContain('Finish adding');expect(ui.output()).not.toContain('Configured models');expect(ui.output()).not.toContain('Help:');expect(ui.output()).not.toContain('Refresh models:');expect(ui.output()).not.toContain('Test configured model:');expect(ui.output()).not.toContain('synthetic-secret');
 }finally{ui.close();}
});

test("model rows do not select or navigate; only Advanced opens the editable JSON",async()=>{
 const entries=['v3','v4flash','v4pro'].map((id,i)=>({model_id:id,protocol:'openai_chat',context_window:(i+1)*32000,max_output_tokens:2048,request_preferences:{}}));
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return listing;return {};},{seed:{...seed,model_id:'v4pro',configuration:{models:entries}}});
 try{
  await ui.send('');expect(ui.output()).toContain('Models (3)');expect(ui.output()).toMatch(/v3\s*\n[^\n]*v4flash\s*\n[^\n]*v4pro/);expect(ui.output()).not.toContain('current');expect(ui.output()).not.toContain('default');
  await move(ui,5);await ui.send('\r');expect(ui.output()).not.toContain('Advanced · configuration JSON\n{');await move(ui,9);await ui.send('\r');expect(ui.output()).toContain('"request_preferences"');expect(ui.footers.at(-1).hidden).toBe(true);expect(calls.some(c=>c[0]==='provider.model.select')).toBe(false);
  const value=document(entries);value.models[1]!.context_window=96000;value.models[1]!.request_preferences={reasoning_effort:'high',body:{top_p:0.8}} as any;
  await replaceDocument(ui,value);await ui.send('\x13');const saved=calls.find(c=>c[0]==='provider.save')[1];expect(saved.model_id).toBe('v4pro');expect(saved.models[1]).toMatchObject({context_window:96000,request_preferences:{reasoning_effort:'high',body:{top_p:0.8}}});
 }finally{ui.close();}
});

test("JSON validation and backend errors retain the edited document; Esc restores the outer draft",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return listing;if(op==='provider.save')throw Error('SYNTHETIC_SAVE_FAILURE');return {};},{seed});
 try{
  await ui.send('');await move(ui,7);await ui.send('\r');await replaceDocument(ui,'{');await ui.send('\x13');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await replaceDocument(ui,document());await ui.send('\x13');expect(ui.output()).toContain('SYNTHETIC_SAVE_FAILURE');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
  await ui.send('\x1b');await ui.send('\x13');expect(calls.filter(c=>c[0]==='provider.save').at(-1)[1].name).toBe('Existing');
 }finally{ui.close();}
});

test("manual multi-line addition deduplicates into the same collection without committing",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return listing;return {};},{seed});
 try{
  await ui.send('');await move(ui,6);await ui.send('\r');await ui.send('\x1b[B');await ui.send('\x1b[B');await ui.send('\r');expect(ui.footers.at(-1).kind).toBe('normal');expect(ui.output()).not.toContain('Add models manually');
  await paste(ui,'old\nmanual-one\nmanual-two\nmanual-one\n');await ui.send('\x13');expect(ui.output()).toContain('Models (3)');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\x13');expect(calls.find(c=>c[0]==='provider.save')[1].models.map((p:any)=>p.model_id)).toEqual(['old','manual-one','manual-two']);
 }finally{ui.close();}
});

test("manual input edits in the candidate list without a second page or duplicated instruction",async()=>{
 const ui=mount(async(op:string)=>op==='provider.models'?listing:{},{seed});
 try{
  await ui.send('');await move(ui,6);await ui.send('\r');
  expect(ui.output()).not.toContain('Existing models are retained');
  await ui.send('\x1b[B');await ui.send('\x1b[B');await ui.send('\r');
  await paste(ui,'custom-a\ncustom-b');await ui.send('\r');
  expect(ui.output()).toContain('custom-a');expect(ui.output()).toContain('custom-b');
  expect(ui.output()).not.toContain('Add models manually');
  await ui.send('\x13');expect(ui.output()).toContain('Models (3)');
 }finally{ui.close();}
});

test("saving JSON returns to the API form and next save uses the new revision",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return listing;if(op==='provider.save')return {provider:{...seed,name:args.name,revision:'r2',configuration:{models:args.models}}};return {};},{seed});
 try{
  await ui.send('');await move(ui,7);await ui.send('\r');await replaceDocument(ui,document());await ui.send('\x13');
  expect(ui.done).toHaveLength(0);expect(ui.footers.at(-1).hidden).toBe(true);
  await ui.send('\x13');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(2);
  expect(calls.filter(c=>c[0]==='provider.save')[1][1]).toMatchObject({expected_revision:'r2',name:'Edited',retain_api_key:true});
  expect(ui.done).toHaveLength(1);
 }finally{ui.close();}
});

test("creating from JSON stays on the API form and later saves update the new connection instead of creating another",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.presets')return {presets:[{id:'mimo',name:'MiMo',base_url:seed.base_url,protocol:'openai_chat'}]};if(op==='provider.models')return listing;if(op==='provider.save')return {provider:{...seed,id:'created-api',name:args.name,revision:'created-r1',configuration:{models:args.models}}};return {};});
 try{
  await ui.send('');await ui.send('MiMo');await ui.send('\r');await ui.send('\r');await ui.send('synthetic-secret');await ui.send('\r');
  await move(ui,6);await ui.send('\r');await replaceDocument(ui,document([{model_id:'a'},{model_id:'b'},{model_id:'manual-c'}]));await ui.send('\x13');
  expect(ui.done).toHaveLength(0);expect(ui.output()).toContain('Models (3)');
  // Saving returns focus to Advanced even when JSON changed the model count.
  await ui.send('\r');expect(ui.footers.at(-1).hidden).toBe(true);await ui.send('\x13');
  const saves=calls.filter(c=>c[0]==='provider.save');expect(saves).toHaveLength(2);
  expect(saves[1][1]).toMatchObject({provider_id:'created-api',expected_revision:'created-r1',creation_id:'',retain_api_key:true});
  expect(ui.done).toHaveLength(0);
 }finally{ui.close();}
});

test("candidate cancellation and refresh preserve configured models and do not select a runtime model",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return listing;return {};},{seed});
 try{await ui.send('');await move(ui,6);await ui.send('\r');await ui.send('\r');await ui.send('\x1b');await ui.send('\x12');await ui.send('\x13');const saved=calls.find(c=>c[0]==='provider.save')[1];expect(saved.models.map((p:any)=>p.model_id)).toEqual(['old']);expect(calls.some(c=>c[0]==='provider.model.select')).toBe(false);}finally{ui.close();}
});

test("refresh is scoped to model collection rows and follows rebinding",async()=>{
 const calls:any[]=[];const before=applicationBindings.snapshot();const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);return op==='provider.models'?listing:{};},{seed});
 try{await ui.send('');await move(ui,1);await ui.send('\x12');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(1);await move(ui,5);await ui.send('\x12');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(2);applicationBindings.configure({version:1,overrides:{'provider.models.refresh':{key:'q',ctrl:true}}});await ui.send('\x12');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(2);await ui.send('\x11');expect(calls.filter(c=>c[0]==='provider.models')).toHaveLength(3);}finally{applicationBindings.configure(before);ui.close();}
});

test("batch selection uses the actual form-save binding instead of a selectable completion action",async()=>{
 const before=applicationBindings.snapshot(),calls:any[]=[];
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);return op==='provider.models'?listing:{};},{seed});
 try{
  await ui.send('');await move(ui,6);await ui.send('\r');await ui.send('\r');
  applicationBindings.configure({version:1,overrides:{'form.save':{key:'s',ctrl:true,shift:true}}});
  await ui.send('\x13');expect(ui.footers.at(-1).kind).toBe('normal');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\x1b[115;6u');expect(ui.output()).toContain('Models (2)');expect(ui.footers.at(-1).hidden).toBe(true);
  expect(ui.output()).not.toContain('Finish adding');expect(calls.some(c=>c[0]==='provider.save')).toBe(false);
  await ui.send('\x1b[115;6u');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);
 }finally{applicationBindings.configure(before);ui.close();}
});

test("removal is a draft edit, keeps navigation bounded and silently falls back to the first remaining model on save",async()=>{
 const models=['one','two'].map(model_id=>({model_id,protocol:'openai_chat',context_window:32000,max_output_tokens:2048,request_preferences:{}})),calls:any[]=[];
 const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);return op==='provider.models'?{models:[]}:{};},{seed:{...seed,model_id:'two',configuration:{models}}});
 try{await ui.send('');await move(ui,6);await ui.send('\x1b[3~');await ui.send('\x1b[B'.repeat(20));await ui.send('\x13');const saved=calls.find(c=>c[0]==='provider.save')[1];expect(saved.model_id).toBe('one');expect(saved.models.map((p:any)=>p.model_id)).toEqual(['one']);}finally{ui.close();}
});

test("help is a command and preserves the outer API draft",async()=>{
 const calls:any[]=[];const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);return op==='provider.models'?{models:[]}:{};},{seed});
 try{await ui.send('');await move(ui,1);await ui.send('\r');await ui.send(' staged');await ui.send('\r');await ui.send('/help');await ui.send('\r');expect(ui.output()).toContain('Provider configuration');await ui.send('\x1b');await ui.send('\x13');expect(calls.find(c=>c[0]==='provider.save')[1].name).toBe('Existing staged');}finally{ui.close();}
});

test("background checks remain bounded and do not prevent saving",async()=>{
 const calls:any[]=[];let resolve:any;const ui=mount(async(op:string,args:any)=>{calls.push([op,args]);if(op==='provider.models')return {models:[]};if(op==='provider.connection')return new Promise(r=>resolve=r);return {};},{seed});
 try{await new Promise(r=>setTimeout(r,800));expect(calls.filter(c=>c[0]==='provider.connection')).toHaveLength(1);await ui.send('\x13');expect(calls.filter(c=>c[0]==='provider.save')).toHaveLength(1);resolve({checks:{model_response:'verified'}});await ui.send('');expect(calls.filter(c=>c[0]==='provider.connection')).toHaveLength(1);}finally{ui.close();}
});
