import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {ProcessList} from "./ProcessList.js";

test("empty subprocess lists render on first entry, after removal and after reopening", async () => {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 80, rows: 24}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const errors: Error[] = [], ids = {current: [] as string[]};
  class Boundary extends React.Component<{children: React.ReactNode}, {failed: boolean}> {
    state = {failed: false};
    static getDerivedStateFromError() {return {failed: true};}
    componentDidCatch(error: Error) {errors.push(error);}
    render() {return this.state.failed ? null : this.props.children;}
  }
  const draw = (processes: Record<string, any>, selected = 0, key = "first") => <InputProvider><Boundary>
    <Box height={stdout.rows} flexDirection="column"><ProcessList key={key} project="empty-render-regression" cols={stdout.columns}
      selIdx={selected} idsRef={ids} loopData={{processes, recoveryAvailable: [], storage: null} as any}/></Box>
  </Boundary></InputProvider>;
  const root = renderSync(draw({}), {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const text = () => {const screen = (instances.get(stdout) as any).frontFrame.screen; return Array.from({length: screen.height}, (_, y) =>
    Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char ?? " ").join("")).join("\n");};
  const check = async (processes: Record<string, any>, selected = 0, key = "first") => {
    root.rerender(draw(processes, selected, key)); await new Promise(resolve => setTimeout(resolve, 70)); expect(errors).toEqual([]);
  };
  try {
    await check({}); expect(text()).toContain("No subprocesses."); expect(ids.current).toEqual([]);
    const supervisor = {process_id: "root", actor_kind: "supervisor", role: "supervisor", created_at: "0"};
    await check({root: supervisor}, 999); expect(text()).toContain("No subprocesses.");
    const worker = {process_id: "worker", parent_id: "root", parent_ids: ["root"], actor_kind: "worker", role: "worker", status: "running", created_at: "1", steps_used: 0, max_steps: 10};
    await check({root: supervisor, worker}); expect(ids.current).toEqual(["worker"]);
    await check({}, 999); expect(text()).toContain("No subprocesses.");
    stdout.columns = 40; stdout.rows = 12; stdout.emit("resize");
    await check({}, 0, "reopened"); expect(text()).toContain("No subprocesses."); expect(ids.current).toEqual([]);
  } finally {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}
});
