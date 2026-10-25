import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Text} from "@anthropic/ink";
import {KeybindingsTab} from "./KeybindingsTab.js";
import {InputProvider, useBindingSettings, useManagedInput} from "../../input/runtime.js";
import {presetSettings} from "../../input/router.js";
import {useGeneralConfig} from "../../hooks/useGeneralConfig.js";
import {useTextInput} from "../../hooks/useTextInput.js";
import {chordLabel} from "../../input/bindings.js";
import type {BackendClient} from "../../backend/client.js";

test("scheme save updates routing and help labels; reopening loads it; reset restores defaults", async () => {
  let stored = "standard";
  const writes: any[] = [], events: any[] = [];
  const client: BackendClient = {ready:true, close(){}, async callTool(operation,args={}) {
    if(operation === "config.get")return {keyboard_preset:stored};
    if(operation === "config.set"){stored=String(args.value);writes.push(args);return {ok:true};}
    return {};
  }};
  const wait = () => new Promise(resolve => setTimeout(resolve,35));
  async function mount() {
    const stdin: any = new PassThrough(); stdin.isTTY=true;stdin.setRawMode=()=>stdin;stdin.ref=()=>stdin;stdin.unref=()=>stdin;
    const stdout: any = new PassThrough();stdout.isTTY=false;stdout.columns=80;stdout.rows=24;
    let output="";stdout.on("data",(s:Buffer)=>{output+=s.toString();});
    const stderr: any=new PassThrough();stderr.on("data",()=>{});
    function Fixture() {
      const settings=useGeneralConfig(client), input=useTextInput();
      const bindings=React.useMemo(()=>presetSettings(settings.keyboardPreset),[settings.keyboardPreset]);
      useBindingSettings(bindings);
      useManagedInput((text,key)=>{if(key.newline){events.push({text,key});return true;}return false;},{priority:1000});
      return <><KeybindingsTab client={client} project="" cmdInput={input} onFooter={()=>{}} report={()=>{}}
        contentFocused shell={{back(){},goToTab(){},tabPrev(){},tabNext(){},leaveContent(){}}}/>
        <Text>{chordLabel("shiftEnter")}</Text></>;
    }
    const root=renderSync(<InputProvider><Fixture/></InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
    await wait();
    return {stdin,output:()=>output,close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
  }
  let terminal=await mount();
  try {
    terminal.stdin.write("\r");await wait();
    terminal.stdin.write("\x1b[C");await wait();
    terminal.stdin.write("\r");await wait();await wait();
    expect(stored).toBe("portable");expect(terminal.output()).toContain("Alt+Enter");
    terminal.stdin.write("\x1b\r");await wait();
    expect(events.at(-1)?.text).toBe("\n");
  } finally {terminal.close();}
  terminal=await mount();
  try {
    expect(terminal.output()).toContain("Portable");
    terminal.stdin.write("\x1b[B");await wait();
    terminal.stdin.write("\r");await wait();await wait();
    expect(stored).toBe("standard");expect(terminal.output()).toContain("Shift+Enter");
    expect(writes.map(w=>w.value)).toEqual(["portable","standard"]);
  } finally {terminal.close();}
});
