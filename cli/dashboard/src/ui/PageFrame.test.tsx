import React, {useState} from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, ScrollBox, Text, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {PageFrame} from "./PageFrame.js";
import {PageHeader} from "./PageHeader.js";
import {pages} from "./pageContract.js";
import {InputProvider, useManagedInput} from "../input/runtime.js";

for (const width of [38, 124]) test(`page frame reserves chrome while long/short content changes at ${width} columns`, async () => {
  const height = 12;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: width, rows: height}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  function Fixture() {
    const [long, setLong] = useState(false);
    useManagedInput(() => {setLong(value => !value);}, {clipboard: false});
    return <PageFrame page={pages.usage} header={<><Text>页框标题</Text><PageHeader items={[{id: "one", label: "页面甲"}, {id: "two", label: "页面乙"}]}
      selected={long ? 1 : 0} width={width - 2} contentFocused={false}/><Text>数据合计</Text></>} footer={<Text>固定底部</Text>}>
      <ScrollBox flexDirection="column" flexGrow={1} minHeight={0}>{Array.from({length: long ? 150 : 2}, (_, index) => <Text key={index}>content-{index}</Text>)}</ScrollBox>
    </PageFrame>;
  }
  const root = renderSync(<InputProvider><Box width={width} height={height} flexDirection="column"><Fixture/></Box></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    for (let index = 0; index < 5; index++) {
      await new Promise(resolve => setTimeout(resolve, 60));
      const screen = (instances.get(stdout) as any).frontFrame.screen;
      const lines = Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? "").join(""));
      expect(lines[1]).toContain("页框标题"); expect(lines[1]).not.toContain("页面甲"); expect(lines[2]).toContain("页面甲");
      expect(lines[2]).toContain("页面乙"); expect(lines[4]).toContain("数据合计"); expect(lines[height - 1]).toContain("固定底部");
      expect(lines.join("\n").split("页面甲").length - 1).toBe(1); stdin.write("\x1b[C");
    }
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
});
