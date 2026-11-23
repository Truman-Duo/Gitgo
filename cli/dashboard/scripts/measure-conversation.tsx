// Developer component measurements. This is not formal-terminal visual QA.
import React, {createRef} from "react";
import {PassThrough} from "node:stream";
import os from "node:os";
import {Box, ScrollBox, renderSync} from "@anthropic/ink";
import type {ScrollBoxHandle} from "@anthropic/ink";
import instances from "../vendor/ink/src/core/instances.js";
import {TurnTimeline} from "../src/components/TurnTimeline.js";
import {ContentViewport} from "../src/ui/ContentViewport.js";
import type {StreamActivity, ToolCallCard} from "../src/types.js";
const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const nodeCount = (node: any): number => 1 + (node.childNodes || []).reduce((n: number, child: any) => n + nodeCount(child), 0);
const count = 600;
const tools: ToolCallCard[] = Array.from({length: count}, (_, index) => ({
  tool_name:"read_file",tool_call_id:"call-"+index,process_id:"actor-"+index%4,target:"file-"+index,
  allowed:true,state:"completed",status_label:"OK",duration_ms:5,timestamp:"2026-11-08T00:00:00Z",
  result_text:"recorded output "+index,
}));
const many: StreamActivity[] = tools.map((_,toolIndex)=>({kind:"tool",toolIndex}));
const diff = "--- a/file.ts\n+++ b/file.ts\n@@ -1,5000 +1,5000 @@\n"
  + Array.from({length:5000},(_,i)=>"-const old"+i+" = 1;\n+const next"+i+" = 2;").join("\n");
const cases = [
  {name:"600-tools-4-processes", activity:many, tools},
  {name:"320000-utf16-char-reasoning", activity:[{kind:"reasoning",text:("中文 e\u0301 reasoning\n").repeat(20000)}] as StreamActivity[], tools:[]},
  {name:"10000-line-diff",activity:[{kind:"tool",toolIndex:0}] as StreamActivity[],tools:[{...tools[0]!,diff}]},
];
const measurements: any[] = [];
const repeatCount=Number(process.env.GITGO_MEASURE_OPEN_CYCLES??3);
if(!Number.isSafeInteger(repeatCount)||repeatCount<3||repeatCount>100)throw Error("Expected 3-100 component open cycles");
for (const sample of cases) {
  const samples: any[] = [];
  for (let iteration=0; iteration<repeatCount; iteration++) {
    Bun.gc(true);
    const before = process.memoryUsage().heapUsed;
    const stdin: any = new PassThrough(); Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
    const stdout: any = new PassThrough(); Object.assign(stdout,{isTTY:false,columns:100,rows:24}); stdout.on("data",()=>{});
    const stderr: any = new PassThrough(); stderr.on("data",()=>{});
    const scrollRef = createRef<ScrollBoxHandle>();
    let delayed = 0;
    const started = performance.now();
    const probe = new Promise<void>(resolve=>setTimeout(()=>{delayed=Math.max(0,performance.now()-started-10);resolve();},10));
    const root=renderSync(<Box width={100} height={24} flexDirection="column"><ContentViewport scrollRef={scrollRef}>
      <ScrollBox ref={scrollRef} flexGrow={1} flexDirection="column">
        <TurnTimeline activity={sample.activity} tools={sample.tools} notices={[]} verbose width={96}/>
      </ScrollBox>
    </ContentViewport></Box>, {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
    const mountMs=performance.now()-started;
    await probe; await wait(60);
    const tree=(instances.get(stdout) as any).rootNode;
    const initialNodes=nodeCount(tree);
    const scrollStarted=performance.now();
    scrollRef.current?.scrollToBottom();
    await wait(60);
    samples.push({mountMs,eventLoopProbeDelayMs:delayed,scrollSettleIncluding60msWait:performance.now()-scrollStarted,
      mountedNodes:initialNodes,finalMountedNodes:nodeCount(tree),retainedHeapDelta:process.memoryUsage().heapUsed-before});
    root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();
    await wait(20); Bun.gc(true);
    samples.at(-1)!.afterCloseHeap=process.memoryUsage().heapUsed;
  }
  measurements.push({case:sample.name,samples});
}
const output={kind:"component-only-not-formal-terminal",runtime:Bun.version,
  platform:process.platform,cpu:os.cpus()[0]?.model,logicalCpus:os.cpus().length,
  viewport:{columns:100,rows:24},measurements};
if (process.argv[2]) await Bun.write(process.argv[2],JSON.stringify(output,null,2)+"\n");
else process.stdout.write(JSON.stringify(output,null,2)+"\n");
