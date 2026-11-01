import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Box, Text} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {ProcessList} from "./ProcessList.js";

class Boundary extends React.Component<{children: React.ReactNode}, {error: string}> {
  state = {error: ""};
  static getDerivedStateFromError(error: Error) {return {error: error.message};}
  render() {return this.state.error ? <Text>{"CAUGHT:" + this.state.error}</Text> : this.props.children;}
}
test("empty first visit, supervisor-only, removal and re-entry keep the process viewport safe", async () => {
  const stdin: any = new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough();Object.assign(stdout,{isTTY:false,columns:100,rows:24});
  const stderr: any = new PassThrough();let output="";stdout.on("data",(chunk: Buffer)=>output+=chunk);stderr.on("data",()=>{});
  const ids={current:[] as string[]};
  const supervisor:any={process_id:"a",actor_kind:"supervisor",role:"supervisor",status:"running",created_at:"1"};
  const worker:any={...supervisor,process_id:"b",actor_kind:"worker",role:"worker",parent_id:"a",steps_used:0,max_steps:10,created_at:"2"};
  function view(processes: any, project="empty-regression", selected=0) {
    return <InputProvider><Boundary><Box width={100} height={20}><ProcessList project={project} loopData={{processes,recoveryAvailable:[]} as any} cols={100} selIdx={selected} idsRef={ids}/></Box></Boundary></InputProvider>;
  }
  const root=renderSync(view({}),{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const wait=()=>new Promise(r=>setTimeout(r,65));
  try {
    await wait();expect(output).toContain("No subprocesses");expect(output).not.toContain("CAUGHT:");
    root.rerender(view({a:supervisor}));await wait();expect(ids.current).toEqual([]);
    root.rerender(view({a:supervisor,b:worker}));await wait();expect(ids.current).toEqual(["b"]);
    root.rerender(view({},"empty-regression",9));await wait();expect(ids.current).toEqual([]);
    root.rerender(view({},"other-empty-project"));await wait();
    root.rerender(view({},"empty-regression"));await wait();
    expect(output).not.toContain("CAUGHT:");
  } finally {root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
