import React, {useState} from "react";
import {test,expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Text,renderSync} from "@anthropic/ink";
import stripAnsi from "strip-ansi";
import {useLoopData} from "./useLoopData.js";
import type {BackendClient} from "../backend/client.js";
function terminal(){const stdin:any=new PassThrough();stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
 const stdout:any=new PassThrough();stdout.isTTY=false;stdout.columns=100;stdout.rows=24;const stderr:any=new PassThrough();let output="";
 stdout.on("data",(c:Buffer)=>{output+=c.toString();});stderr.on("data",()=>{});
 return {stdin,stdout,stderr,text:()=>stripAnsi(output)};}
const wait=()=>new Promise(r=>setTimeout(r,100));
const snapshot={processes:{},main_conversation:[],agent_conversations:{},storage:{level:"ok"}};
test("runtime errors stop loading, stay scoped to their project, and disappear after leaving it",async()=>{
 const io=terminal();let switchProject!:(value:string|null)=>void;
 const client:BackendClient={ready:true,close(){},async callTool(_operation,args={}){if(args.project==="gone")throw new Error("PROJECT_NOT_FOUND");return snapshot;}};
 function Fixture(){const [project,setProject]=useState<string|null>("gone");switchProject=setProject;const data=useLoopData(client,project);return <Text>{JSON.stringify({loading:data.loading,error:data.error})}</Text>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{await wait();expect(io.text()).toContain('"loading":false,"error":"PROJECT_NOT_FOUND"');switchProject(null);await wait();expect(io.text().split("\n").filter(Boolean).at(-1)).toContain('"error":null');switchProject("ready");await wait();expect(io.text().split("\n").filter(Boolean).at(-1)).toContain('"loading":false,"error":null');}
 finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});
test("lifecycle bursts coalesce while one status request is in flight",async()=>{
 const io=terminal();let notify!:(value:any)=>void;let resolveFirst!:(v:any)=>void;let calls=0;
 const client:BackendClient={ready:true,close(){},subscribeEvents(listener){notify=listener;return()=>{};},async callTool(){calls++;if(calls===1)return new Promise(r=>{resolveFirst=r;});return snapshot;}};
 function Fixture(){const data=useLoopData(client,"demo");return <Text>{data.loading?"loading":"ready"}</Text>;}
 const root=renderSync(<Fixture/>,{...io,patchConsole:false,exitOnCtrlC:false});
 try{await wait();for(let i=0;i<30;i++)notify({event:"state_changed",project:"demo"});await wait();expect(calls).toBe(1);resolveFirst(snapshot);await wait();expect(calls).toBe(2);expect(io.text()).toContain("ready");}
 finally{root.unmount();root.cleanup();io.stdin.destroy();io.stdout.destroy();io.stderr.destroy();}
});
