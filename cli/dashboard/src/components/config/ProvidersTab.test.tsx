import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { renderSync } from "@anthropic/ink";
import type { BackendClient } from "../../backend/client.js";
import { useTextInput } from "../../hooks/useTextInput.js";
import { InputProvider } from "../../input/runtime.js";
import { ProvidersTab } from "./ProvidersTab.js";
import chalk from "chalk";
import instances from "../../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../../vendor/ink/src/core/screen.js";

test("one provider tree owns separate two-slot routes and a standalone global switch",async()=>{
 const io=terminal(),mutations:any[]=[];let enabled=false,revision=0,active='main';const routes:Record<string,string[]>={main:[],backup:['third']};
 const client:BackendClient={ready:true,close(){},async callTool(op,args:any={}){
  if(op==='provider.status')return {providers:[{id:'main',name:'A',model_id:'a'},{id:'backup',name:'B',model_id:'b'},{id:'third',name:'C',model_id:'c'}],active_provider:active,failover_enabled:enabled,failover_routes:routes,failover_revision:'r'+revision};
  if(op==='runtime.status')return {providers:[{id:'main',breaker_state:'open'},{id:'backup',breaker_state:'closed'}]};
  if(op==='provider.failover.configure'){mutations.push(args);enabled=args.enabled;if(args.provider_id)routes[args.provider_id]=args.order;revision++;return {};}
  if(op==='provider.switch'){active=args.provider_id;return {};}
  return {};
 }};
 function Fixture(){const input=useTextInput('');return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input} onFooter={()=>{}} report={()=>{}} contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false}),send=async(s:string)=>{io.stdin.write(s);await new Promise(r=>setTimeout(r,90));};
 try{
  await send('');expect(io.output()).toContain('Automatic failover');expect(io.output()).not.toContain('Backup 1');expect(io.output()).not.toContain('Backup 2');expect(io.output()).not.toContain('Enter models');expect(io.output()).not.toContain('Esc back');
  await send('\r');await send('\x1b[C');expect(mutations).toHaveLength(0);await send('\r');expect(mutations[0]).toEqual({enabled:true,expected_revision:'r0'});
  await send('\x1b[B');await send('\x1b[B');await send('\r');await send('\x1b[C');await send('\r');expect(mutations[1]).toEqual({enabled:true,provider_id:'main',order:['backup'],expected_revision:'r1'});
  await send('\x1b[B');await send('\r');await send('\x1b[C');await send('\r');expect(mutations[2].order).toEqual(['backup','third']);
  await send('\x1b[B');await send('\x1b[B');await send('\r');await send('\x1b[D');await send('\x1b[D');await send('\r');expect(mutations[3]).toEqual({enabled:true,provider_id:'backup',order:[],expected_revision:'r3'});expect(routes.main).toEqual(['backup','third']);
  await send('\x1b[A');await send('\r');expect(active).toBe('backup');expect(routes.main).toEqual(['backup','third']);expect(routes.backup).toEqual([]);
  expect(io.output()).toContain('paused after failures');expect(io.output()).toContain('routing enabled');expect(io.output()).not.toContain('unobserved');
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

test("backups are gray when failover is off but remain selectable and editable without numbered labels",async()=>{
 const previous=chalk.level;chalk.level=3;const io=terminal();io.stdout.isTTY=true;
 let enabled=false,revision=0;const writes:any[]=[],routes={a:['b','c']};
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){
  if(op==='provider.status')return {providers:[{id:'a',name:'API A',model_id:'one'},{id:'b',name:'Backup B',model_id:'two'},{id:'c',name:'Backup C',model_id:'three'}],active_provider:'a',failover_enabled:enabled,failover_routes:routes,failover_revision:'r'+revision};
  if(op==='provider.failover.configure'){writes.push(args);enabled=args.enabled;revision++;return {};}
  return {};
 }};
 function Fixture(){const input=useTextInput('');return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input} onFooter={()=>{}} report={()=>{}} contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,85));};
 const frame=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join(''));};
 const backupCodes=()=>{const lines=frame(),y=lines.findIndex(line=>line.trim()==='Backup B'),x=lines[y]!.indexOf('Backup B');return ink.stylePool.get(cellAt(ink.frontFrame.screen,x,y)!.styleId).map((s:any)=>s.code).join('');};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');expect(frame().some(line=>line.includes('Backup 1')||line.includes('Backup 2'))).toBe(false);
  expect(backupCodes()).toContain('38;2;168;168;168');
  await send('\x1b[B');await send('\x1b[B');expect(backupCodes()).toContain('38;2;168;168;168');
  await send('\r');await send('\x1b[C');await send('\x1b[D');await send('\r');
  expect(writes[0]).toEqual({enabled:false,provider_id:'a',order:['b','c'],expected_revision:'r0'});
  await send('\x1b[A');await send('\x1b[A');await send('\r');await send('\x1b[C');await send('\r');
  expect(enabled).toBe(true);expect(backupCodes()).not.toContain('38;2;168;168;168');
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();chalk.level=previous;}
});

test("local provider opens the existing field form without asking for a fake key", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const client: BackendClient = {
    ready: true, close() {},
    async callTool() { return {providers: [], active_provider: ""}; },
  };
  function Fixture() {
    const input = useTextInput("/local");
    return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input}
      onFooter={() => {}} report={() => {}} contentFocused
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 80)); stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 100));
    expect(output()).toContain("127.0.0.1:11434/v1");
    expect(output()).toContain("not required");
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});

test("API switching is single-flight, keeps list positions and acknowledges real success without full-screen clears",async()=>{
 const io=terminal();io.stdout.isTTY=true;let active='a',finish:any;const writes:string[]=[],footers:any[]=[];let emitted='';io.stdout.on('data',(b:Buffer)=>emitted+=b);
 const client:any={ready:true,close(){},async callTool(op:string,args:any={}){
  if(op==='provider.status')return {active_provider:active,providers:[{id:'a',name:'API A',model_id:'one'},{id:'b',name:'API B',model_id:'two'}]};
  if(op==='provider.switch'){writes.push(args.provider_id);return new Promise(resolve=>finish=()=>{active=args.provider_id;resolve({status:'switched',active_provider:active});});}
  return {};
 }};
 function Fixture(){const input=useTextInput('');return <InputProvider><ProvidersTab client={client} project="" cmdInput={input} onFooter={f=>footers.push(f)} report={()=>{}} contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false}),ink:any=instances.get(io.stdout);
 const send=async(text:string)=>{io.stdin.write(text);await new Promise(r=>setTimeout(r,85));};
 const frame=()=>{const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||' ').join(''));};
 try{
  ink.setAltScreenActive(true);ink.onRender();await send('');await send('\x1b[B');await send('\r');expect(writes).toEqual([]);
  await send('\x1b[B');await send('\x1b[B');await send('\x1b[B');
  const y=frame().findIndex(line=>line.includes('API B'));emitted='';await send('\r');await send('\r');await send('\r');
  expect(writes).toEqual(['b']);expect(frame().findIndex(line=>line.includes('API B'))).toBe(y);expect(frame().some(line=>line.includes('Loading'))).toBe(false);
  expect(footers.at(-1).statusText).toContain('Applying');expect(emitted).not.toMatch(/\x1b\[2J|\x1b\[\?1049[hl]|\x1bc/u);
  finish();await send('');expect(frame().findIndex(line=>line.includes('API B'))).toBe(y);expect(frame().join('\n')).toContain('Switched to API B');
  await send('\r');expect(writes).toEqual(['b']);
 }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

function terminal() {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = false; stdout.columns = 100; stdout.rows = 30;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  stderr.on("data", () => {});
  return {stdin, stdout, stderr, output: () => output};
}

test("missing runtime route history does not add an unexplained health label to either column",async()=>{
 const io=terminal();
 const client:BackendClient={ready:true,close(){},async callTool(op){return op==='provider.status'?{providers:[{id:'a',name:'API A',model_id:'one'},{id:'b',name:'API B',model_id:'two'}],active_provider:'a',failover_order:[]}:{};}};
 function Fixture(){const input=useTextInput('');return <InputProvider><ProvidersTab client={client} project="" cmdInput={input} onFooter={()=>{}} report={()=>{}} contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{await new Promise(r=>setTimeout(r,100));io.stdin.write('\r');await new Promise(r=>setTimeout(r,100));expect(io.output()).toContain('Automatic failover');expect(io.output()).toContain('API B');expect(io.output()).not.toContain('unobserved');expect(io.output()).not.toContain('routing enabled');}finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

test("editing a saved API discovers models automatically and retains the selected model until save",async()=>{
  const io=terminal(),calls:any[]=[],footers:any[]=[];
  const client:BackendClient={ready:true,close(){},async callTool(operation,args:any={}){
    calls.push([operation,args]);
    if(operation==="provider.status")return {active_provider:"mimo",providers:[{id:"mimo",name:"MiMo",base_url:"https://api.test/v1",model_id:"old-model",revision:"r1",protocol:"openai_chat"}]};
    if(operation==="runtime.status"||operation==="provider.presets")return {};
    if(operation==="provider.models")return {receipt_id:"model-list",models:[{id:"new-model",name:"New model",limits:{}}]};
    throw Error(operation);
  }};
  function Fixture(){const input=useTextInput("/edit");return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input} contentFocused onFooter={footer=>footers.push(footer)} report={()=>{}} shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
  const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false});
  try{
    await new Promise(r=>setTimeout(r,80));io.stdin.write("\r");await new Promise(r=>setTimeout(r,700));
    expect(footers.some(footer=>footer?.suggestions?.some((item:any)=>item.label==="/models"))).toBe(false);
    expect(io.output()).toContain("Edit API connection");expect(io.output()).toContain("old-model");
    expect(io.output()).not.toContain("unobserved");
    expect(calls.find(c=>c[0]==="provider.models")[1].draft).toMatchObject({provider_id:"mimo",retain_api_key:true});
    expect(calls.some(c=>c[0]==="provider.save")).toBe(false);
  }finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});

test("provider /new chooses a preset then enters the staged NormalBar form", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const footers: any[] = [];
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/new");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={value => { if (value) footers.push(value); }} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 60));
    expect(output()).toContain("Choose Provider");
    stdin.write("\r"); await new Promise(resolve => setTimeout(resolve, 60));
    expect(output()).toContain("Configure API connection");
    expect(output()).toContain("Name");
    // Enter selects editing; selecting the form alone does not edit values.
    stdin.write("\r"); await new Promise(resolve => setTimeout(resolve, 60));
    expect(footers.some(footer => footer.kind === "normal")).toBe(true);
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("provider partial command executes the highlighted fixed operation on one Enter", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/n");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={() => {}} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 60));
    expect(output()).toContain("Choose Provider");
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("provider edit cancellation clears the shared CommandBar buffer", async () => {
  const {stdin, stdout, stderr} = terminal();
  const footers: any[] = [];
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/new");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={value => { if (value) footers.push(value); }} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("draft provider");
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\u001b");
    // Ink distinguishes a lone Escape from the prefix of a CSI sequence after
    // a short input timeout. Wait for that parse boundary and the following
    // React effect that republishes the CommandBar footer.
    await new Promise(resolve => setTimeout(resolve, 180));
    const latest = footers.at(-1);
    expect(latest.hidden).not.toBe(true);
    expect(latest.kind).toBe("command");
    expect(latest.cmdInput.value).toBe("");
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("provider fields stage on Enter, undo one field and save once with visible failures",async()=>{
  const {stdin,stdout,stderr,output}=terminal();const saves:any[]=[];const footers:any[]=[];
  let release:any;
  const client:BackendClient={ready:true,close(){},async callTool(operation,args:any={}){
    if(operation==="provider.status")return {providers:[],active_provider:""};
    if(operation==="runtime.status")return {};
    if(operation==="provider.save"){
      saves.push(args);
      if(saves.length===1)throw new Error("LOCAL_ENDPOINT_INVALID: endpoint is unavailable");
      return new Promise(resolve=>release=resolve);
    }
    throw new Error(operation);
  }};
  function Fixture(){const input=useTextInput("/local");return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input} onFooter={value=>footers.push(value)} report={()=>{}} contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/></InputProvider>;}
  const root=renderSync(<Fixture/>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const send=async(keys:string)=>{stdin.write(keys);await new Promise(r=>setTimeout(r,70));};
  try{
    await send("");await send("\r");await send("\t");await send("\r");await send("local-test");await send("\r");
    expect(saves).toHaveLength(0);expect(output()).not.toContain("▸");
    await send("\r");await send("-discard");await send("\x1b");
    for(let i=0;i<4;i++)await send("\t");
    await send("\r");await send("\r");await send("local-model");await send("\x13");
    expect(saves).toHaveLength(0);
    await send("\x13");expect(saves).toHaveLength(1);expect(output()).toContain("LOCAL_ENDPOINT_INVALID");
    expect(saves[0]).toMatchObject({name:"local-test",model_id:"local-model",protocol:"openai_chat",kind:"local",auth_mode:"none"});
    await send("\x13");await send("\x13");expect(saves).toHaveLength(2);
    release({ok:true});await send("");expect(output()).toContain("API connection and models saved");
    expect(footers.at(-1)?.hidden).not.toBe(true);
    expect(footers.at(-1)?.kind).toBe("command");
  }finally{root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
