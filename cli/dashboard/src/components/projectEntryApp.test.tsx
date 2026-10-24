import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import stripAnsi from "strip-ansi";
import {App} from "./App.js";
import {InputProvider} from "../input/runtime.js";
import type {BackendClient} from "../backend/client.js";

// Full application/input integration; this does not replace real terminal visual review.
test("project list renders three groups and rejects missing entry before mounting chat", async () => {
  const stdin: any = new PassThrough(); stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
  const stdout: any = new PassThrough(); stdout.isTTY=false;stdout.columns=120;stdout.rows=36;
  const stderr: any = new PassThrough();let output="";stdout.on("data",(chunk:Buffer)=>{output+=chunk.toString();});stderr.on("data",()=>{});
  let visibleReady=true; const listeners=new Set<(event:any)=>void>(); const notify=(event:any)=>{for(const listener of listeners)listener(event);};
  const calls: Array<{operation:string;args:any}>=[];
  const client: BackendClient={ready:true,close(){},subscribeEvents(listener){listeners.add(listener);return()=>{listeners.delete(listener);};},async callTool(operation,args={}) {
    calls.push({operation,args});
    if(operation==="project.overview")return {projects:[
      {name:"ready",workspace:"C:/ready",runtime_summary:{state_available:true,daemon_online:false}},
      {name:"gone",workspace:"C:/gone",runtime_summary:{state_available:false,summary_error:"Workspace does not exist"}},
      {name:"busy",workspace:"C:/busy",runtime_summary:{state_available:true,daemon_online:true,active_process_count:1}},
    ].filter(item=>visibleReady||item.name!=="ready")};
    if(operation==="project.enter") {
      if(args.name==="gone")throw new Error("WORKSPACE_NOT_FOUND: Workspace does not exist: C:/gone");
      return {ok:true,name:args.name};
    }
    if(operation==="config.get") return {language:"en",verbose:false,auto_compact:true};
    if(operation==="runtime.status")return {daemon_online:false,processes:{},state_available:true};
    return {};
  }};
  const root=renderSync(<InputProvider><App client={client}/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const wait=()=>new Promise(r=>setTimeout(r,100));
  try {
    await wait();
    const frame=stripAnsi(output);
    expect(frame).toContain("Running (1)");
    expect(frame).toContain("Completed / Waiting (1)");
    expect(frame).toContain("Unavailable (1)");
    expect(frame).not.toContain("New (");
    // running -> idle -> unavailable, sharing the same order as Overview.
    stdin.write("\x1b[B");await wait();stdin.write("\x1b[B");await wait();
    stdin.write("\r");await wait();
    expect(calls).toContainEqual({operation:"project.enter",args:{name:"gone"}});
    expect(stripAnsi(output)).toContain("WORKSPACE_NOT_FOUND: Workspace does not exist: C:/gone");
    expect(calls.some(c=>c.operation==="runtime.status"&&c.args.project==="gone")).toBe(false);
    expect(calls.some(c=>c.operation==="runtime.chat")).toBe(false);
    stdin.write("\x1b[A");await wait();stdin.write("\r");await wait();
    expect(calls).toContainEqual({operation:"project.enter",args:{name:"ready"}});
    expect(calls.some(c=>c.operation==="runtime.status"&&c.args.project==="ready")).toBe(true);
    visibleReady=false;notify({event:"project_lifecycle_changed",project:"ready",state:"archived"});
    await new Promise(resolve=>setTimeout(resolve,250));
    const queries=calls.filter(c=>c.operation==="runtime.status").length;
    expect(stripAnsi(output)).toContain("left the active list");
    await new Promise(resolve=>setTimeout(resolve,150));
    expect(calls.filter(c=>c.operation==="runtime.status")).toHaveLength(queries);
    expect(stripAnsi(output).slice(-4000)).not.toContain("Runtime status unavailable:");
  } finally {root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
