import React from "react";
import {expect, spyOn, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, Text, renderSync} from "@anthropic/ink";
import {useSharedClock} from "./useSharedClock.js";

test("presentation clocks share one active timer and stop it after the last subscriber leaves", async () => {
  const original = globalThis.setInterval;
  const created: ReturnType<typeof setInterval>[] = [];
  const cleared: any[] = [];
  const start = spyOn(globalThis, "setInterval").mockImplementation(((...args: any[]) => {
    const id = (original as any)(...args);
    if (args[1] === 137) created.push(id);
    return id;
  }) as any);
  const originalClear = globalThis.clearInterval;
  const stop = spyOn(globalThis, "clearInterval").mockImplementation(((id: any) => {cleared.push(id); originalClear(id);}) as any);
  const snapshots = new Set<number>();
  function Probe() {const now = useSharedClock(137); snapshots.add(now); return <Text>{String(now)}</Text>;}
  const stdin: any = new PassThrough(); Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough(); Object.assign(stdout,{isTTY:false,columns:90,rows:30}); stdout.on("data",()=>{});
  const stderr: any = new PassThrough(); stderr.on("data",()=>{});
  const root = renderSync(<Box flexDirection="column">{Array.from({length:20},(_,index)=><Probe key={index}/>)}</Box>,
    {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  try {
    await new Promise(resolve=>setTimeout(resolve,200));
    expect(created.length).toBe(1);
    expect(snapshots.size).toBeGreaterThan(1);
    root.rerender(<Box/>); await new Promise(resolve=>setTimeout(resolve,30));
    expect(cleared).toContain(created[0]);
  } finally {root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();start.mockRestore();stop.mockRestore();}
});
