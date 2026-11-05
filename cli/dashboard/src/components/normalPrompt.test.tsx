import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {CommandBar} from "./CommandBar.js";
import type {Scene} from "../state/store.js";

test("ordinary chat and overlay editors use the shared chevron, without changing command or hidden bars", async () => {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 80, rows: 15}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  function Fixture({scene, kind}: {scene: Scene; kind?: "normal" | "command" | "hidden"}) {
    const input = useTextInput("sample"), command = useTextInput("/help");
    const footerOverride = kind === "hidden" ? {hidden: true as const} : kind ? {kind, cmdInput: input, suggestions: [], suggestionIdx: 0, cmdResult: "", statusText: ""} : null;
    return <Box width={80} height={15} flexDirection="column"><CommandBar width={78} mode={kind === "command" ? "COMMAND" : "NORMAL"}
      scene={scene} textInput={input} cmdInput={command} suggestions={[]} suggestionIdx={0} cmdResult="" statusText="" footerOverride={footerOverride}/></Box>;
  }
  const root = renderSync(<Fixture scene="workspace"/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const text = () => {const screen = (instances.get(stdout) as any).frontFrame.screen; return Array.from({length: screen.height}, (_, y) =>
    Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? " ").join("")).join("\n");};
  try {
    for (const scene of ["workspace", "agent_detail", "projects"] as Scene[]) {
      root.rerender(<Fixture scene={scene} kind={scene === "projects" ? "normal" : undefined}/>); await new Promise(resolve => setTimeout(resolve, 60));
      expect(text()).toContain("› sample"); expect(text()).not.toContain("▸");
    }
    root.rerender(<Fixture scene="workspace" kind="command"/>); await new Promise(resolve => setTimeout(resolve, 60));
    expect(text()).not.toContain("›");
    root.rerender(<Fixture scene="workspace" kind="hidden"/>); await new Promise(resolve => setTimeout(resolve, 60));
    expect(text().trim()).toBe("");
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
});
