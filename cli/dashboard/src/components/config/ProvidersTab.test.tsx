import React from "react";
import { expect, test } from "bun:test";
import { PassThrough } from "node:stream";
import { renderSync } from "@anthropic/ink";
import type { BackendClient } from "../../backend/client.js";
import { useTextInput } from "../../hooks/useTextInput.js";
import { InputProvider } from "../../input/runtime.js";
import { ProvidersTab } from "./ProvidersTab.js";

test("backup selection, failover switch and health use native config operations", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const mutations: any[] = [];
  let enabled = false;
  let order: string[] = [];
  const client: BackendClient = {
    ready: true, close() {},
    async callTool(operation, args: any = {}) {
      if (operation === "provider.status") return {
        providers: [{id: "main", name: "Main", model_id: "main"}, {id: "backup", name: "Backup", model_id: "backup"}],
        active_provider: "main", failover_enabled: enabled, failover_order: order,
      };
      if (operation === "runtime.status") return {providers: [{id: "main", breaker_state: "open"}, {id: "backup", breaker_state: "closed"}]};
      if (operation === "provider.failover.configure") {
        mutations.push(args); enabled = args.enabled; order = args.order; return {status: "configured"};
      }
      throw new Error(operation);
    },
  };
  function Fixture() {
    const input = useTextInput("");
    return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input}
      onFooter={() => {}} report={() => {}} contentFocused
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const send = async (keys: string) => { stdin.write(keys); await new Promise(resolve => setTimeout(resolve, 100)); };
  try {
    await new Promise(resolve => setTimeout(resolve, 100));
    await send("\r"); // same entry into existing dual column view
    await send("\u001b[C");
    await send("\r");
    await send("\u001b[C"); // preview on; no command or mutation
    expect(mutations).toHaveLength(0);
    await send("\r");
    expect(mutations.at(-1)).toEqual({enabled: true, order: []});
    await send("\u001b[B");
    await send("\r");
    await send("\u001b[C"); // enabled at priority 1
    await send("\r");
    expect(mutations.at(-1)).toEqual({enabled: true, order: ["backup"]});
    expect(output()).toContain("Automatic Failover: on");
    expect(output()).toContain("open"); expect(output()).toContain("closed");
    await send("\u001b[A"); await send("\r"); await send("\u001b[D");
    await send("\u001b"); // cancel off preview
    expect(mutations).toHaveLength(2);
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});

test("local provider opens the existing field form without asking for a fake key", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const client: BackendClient = {
    ready: true, close() {},
    async callTool() { return {providers: [], active_provider: ""}; },
  };
  function Fixture() {
    const input = useTextInput("/local");
    return <InputProvider><ProvidersTab client={client} project="test" cmdInput={input}
      onFooter={() => {}} report={() => {}} contentFocused
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 80)); stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 100));
    expect(output()).toContain("127.0.0.1:11434/v1");
    expect(output()).toContain("not required");
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});

function terminal() {
  const stdin: any = new PassThrough();
  stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough();
  stdout.isTTY = false; stdout.columns = 100; stdout.rows = 30;
  const stderr: any = new PassThrough();
  let output = "";
  stdout.on("data", (chunk: Buffer) => { output += chunk.toString("utf8"); });
  stderr.on("data", () => {});
  return {stdin, stdout, stderr, output: () => output};
}

test("provider /new enters a field-by-field NormalBar form", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const footers: any[] = [];
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/new");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={value => { if (value) footers.push(value); }} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 60));
    expect(output()).toContain("New Provider");
    expect(output()).toContain("Name");
    expect(footers.some(footer => footer.kind === "normal")).toBe(true);
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("provider partial command executes the highlighted fixed operation on one Enter", async () => {
  const {stdin, stdout, stderr, output} = terminal();
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/n");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={() => {}} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 60));
    expect(output()).toContain("New Provider");
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});

test("provider edit cancellation clears the shared CommandBar buffer", async () => {
  const {stdin, stdout, stderr} = terminal();
  const footers: any[] = [];
  const client: BackendClient = {
    ready: true,
    callTool: async (operation) => operation === "provider.status"
      ? {providers: [], active_provider: ""} : {ok: true},
    close: () => {},
  };
  function Fixture() {
    const input = useTextInput("/new");
    return <InputProvider><ProvidersTab
      client={client} project="test" cmdInput={input}
      onFooter={value => { if (value) footers.push(value); }} report={() => {}}
      shell={{back() {}, goToTab() {}, tabPrev() {}, tabNext() {}, leaveContent() {}}}
      contentFocused
    /></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\r");
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("draft provider");
    await new Promise(resolve => setTimeout(resolve, 40));
    stdin.write("\u001b");
    // Ink distinguishes a lone Escape from the prefix of a CSI sequence after
    // a short input timeout. Wait for that parse boundary and the following
    // React effect that republishes the CommandBar footer.
    await new Promise(resolve => setTimeout(resolve, 180));
    const latest = footers.at(-1);
    expect(latest.kind).toBe("command");
    expect(latest.cmdInput.value).toBe("");
  } finally {
    root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();
  }
});
