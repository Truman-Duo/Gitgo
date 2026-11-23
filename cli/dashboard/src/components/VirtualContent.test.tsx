import React, {createRef, useState} from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, Text, ScrollBox, renderSync, useTerminalSize, type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {ContentViewport} from "../ui/ContentViewport.js";
import {VirtualContent} from "./VirtualContent.js";
const wait = () => new Promise(resolve => setTimeout(resolve, 100));
const text = (node: any): string => node.nodeValue ?? (node.childNodes ?? []).map(text).join("");
test("nested virtual content uses the parent's offset, preserves resize anchors and leaves following content reachable", async () => {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY:true,setRawMode(){},ref(){},unref(){}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY:true,columns:100,rows:16}); stdout.on("data",()=>{});
  const stderr: any = new PassThrough(); stderr.on("data",()=>{});
  const scrollRef = createRef<ScrollBoxHandle>();
  let append: () => void = () => {};
  let growAbove: () => void = () => {};
  function Fixture() {
    const {columns} = useTerminalSize();
    const [count, setCount] = useState(1000); append = () => setCount(value => value + 10);
    const [extra, setExtra] = useState(0); growAbove = () => setExtra(8);
    const rows = columns < 80 ? 8 : 4;
    const keys = Array.from({length:count},(_,index)=>"INNER_"+index);
    return <Box width="100%" height={16} flexDirection="column"><ContentViewport scrollRef={scrollRef}>
      <ScrollBox ref={scrollRef} flexGrow={1} flexDirection="column">
        <Box height={120} flexShrink={0}><Text>PREFIX</Text></Box>
        <VirtualContent itemKeys={keys} estimateRows={rows} layoutKey={columns}>
          {index=><Box height={rows + (index === 99 ? extra : 0)} flexShrink={0}><Text>{keys[index]}</Text></Box>}
        </VirtualContent>
        <Box height={40} flexShrink={0}><Text>FOLLOWING CONTENT</Text></Box>
      </ScrollBox>
    </ContentViewport></Box>;
  }
  const root = renderSync(<Fixture/>, {stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
  const mounted = () => text((instances.get(stdout) as any).rootNode);
  try {
    await wait(); scrollRef.current!.scrollTo(520); await wait();
    expect(mounted()).toContain("INNER_100");
    expect(mounted()).not.toContain("INNER_999");
    expect((mounted().match(/INNER_/g)||[]).length).toBeLessThan(30);
    expect(scrollRef.current!.getScrollTop()).toBe(520);
    growAbove(); await wait();
    expect(scrollRef.current!.getScrollTop()).toBe(528);
    append(); await wait();
    expect(scrollRef.current!.getScrollTop()).toBe(528);
    expect(scrollRef.current!.isSticky()).toBe(false);
    stdout.columns=50; stdout.emit("resize"); await wait();
    expect(mounted()).toContain("INNER_100");
    expect(scrollRef.current!.getScrollTop()).toBe(928);
    scrollRef.current!.scrollTo(120+1010*8+8); await wait();
    expect(scrollRef.current!.getScrollTop()).toBeGreaterThanOrEqual(120+1010*8+8-1);
    expect(mounted()).toContain("FOLLOWING CONTENT");
  } finally {root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}
});
