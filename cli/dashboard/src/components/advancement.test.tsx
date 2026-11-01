import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Text} from "@anthropic/ink";
import {InputProvider, useManagedInput, InputLayer} from "../input/runtime.js";
import {applicationBindings, isBindingAction} from "../input/router.js";
import {useAdvancement} from "../hooks/useAdvancement.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {CommandBar} from "./CommandBar.js";
import type {BackendClient} from "../backend/client.js";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";

// Uses the real Ink decoder, CommandBar and mode controller. This is component
// regression evidence, not a substitute for formal terminal visual acceptance.
test("status mode waits for commit, keeps editor color, exposes failure, and form owns Shift+Tab", async () => {
  const stdin:any = new PassThrough(); stdin.isTTY=true; stdin.setRawMode=()=>stdin; stdin.ref=()=>stdin; stdin.unref=()=>stdin;
  const stdout:any = new PassThrough(); stdout.isTTY=true; stdout.columns=90; stdout.rows=24;
  const stderr:any = new PassThrough(); stderr.on("data",()=>{}); stdout.on("data",()=>{});
  let accept: ((value:any)=>void) | undefined, reject: ((error:Error)=>void) | undefined;
  let writes = 0, formBack = 0;
  let updateForm: ((value:boolean)=>void) | undefined;
  let updateEditor: ((value:"NORMAL"|"COMMAND")=>void) | undefined;
  const client:BackendClient = {ready:true,close(){},async callTool(operation,args={}) {
    expect(operation).toBe("runtime.advancement");
    if (!args.mode) return {mode:"auto",revision:0,phase:"effective",scope_id:"task",process_id:"root"};
    writes++;
    return new Promise((resolve,fail)=>{accept=resolve;reject=fail;});
  }};
  function Form(){useManagedInput((input,key)=>{if(key.tab&&key.shift)formBack++;});return <Text>Form</Text>;}
  function Fixture(){
    const advancement=useAdvancement(client,"isolated"),text=useTextInput(),command=useTextInput();
    const [form,setForm]=React.useState(false);updateForm=setForm;
    const [editor,setEditor]=React.useState<"NORMAL"|"COMMAND">("NORMAL");updateEditor=setEditor;
    useManagedInput((input,key)=>{
      if(!isBindingAction("advancement.toggle",input,key))return false;
      void advancement.toggle();return true;
    },{priority:10,bindingContext:"task",clipboard:false});
    return <>{form?<InputLayer priority={100} bindingContext="form"><Form/></InputLayer>:null}
      <CommandBar width={90} mode={editor} advancement={advancement.value?.mode}
        advancementPending={advancement.pending} textInput={text} cmdInput={command}
        cmdResult={advancement.error} statusText="Completed" contextPct="0.1%" suggestions={[]} suggestionIdx={0} scene="workspace"/>
    </>;
  }
  const before=applicationBindings.snapshot();applicationBindings.configure({version:1,overrides:{}});
  const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const wait=()=>new Promise(r=>setTimeout(r,90));
  const boxes=():any[]=>{const found:any[]=[];const walk=(n:any)=>{if(n.style?.borderColor&&n.style?.borderStyle==="single")found.push(n);for(const c of n.childNodes||[])walk(c);};walk((instances.get(stdout) as any).rootNode);return found;};
  const footer=()=>{const screen=(instances.get(stdout) as any).frontFrame.screen;return Array.from({length:screen.height},(_,y)=>Array.from({length:screen.width},(_,x)=>cellAt(screen,x,y)?.char??" ").join("")).find(line=>line.includes("0.1%"))||"";};
  try{
    await wait(); await wait(); await wait(); const border=boxes()[0].style.borderColor;
    expect(border).toBe("#0071db");
    expect(boxes()[0].style.borderText).toBeUndefined();
    expect(footer()).toMatch(/0\.1% · AUTO/);expect(footer()).not.toContain("Completed");
    stdin.write("\x1b[Z");await wait();expect(writes).toBe(1);
    expect(footer()).toContain("AUTO");
    stdin.write("\x1b[Z".repeat(20));await wait();expect(writes).toBe(1);
    accept?.({mode:"manual",revision:1,phase:"effective",scope_id:"task",process_id:"root"});await wait();
    expect(footer()).toContain("MANUAL");
    expect(boxes()[0].style.borderColor).toBe(border);
    updateEditor?.("COMMAND");await wait();await wait();await wait();
    expect(boxes()[0].style.borderColor).toBe("#208b4e");
    expect(footer()).toContain("MANUAL");
    updateForm?.(true);await wait();stdin.write("\x1b[Z");await wait();expect(formBack).toBe(1);expect(writes).toBe(1);
    updateForm?.(false);await wait();stdin.write("\x1b[Z");await wait();expect(writes).toBe(2);
    reject?.(new Error("commit failed"));await wait();
    expect(footer()).toContain("MANUAL");
    expect(boxes()[0].style.borderColor).toBe("#208b4e");
  } finally {root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();applicationBindings.configure(before);}
});
