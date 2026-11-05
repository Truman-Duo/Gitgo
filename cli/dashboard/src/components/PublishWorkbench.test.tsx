import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import chalk from "chalk";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {selectedRows,focusedHeader} from "../testing/selectedRows.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {PublishWorkspacePanel} from "./PublishWorkspacePanel.js";
import {createPublishStateStore} from "../state/publishLocalState.js";

// Standalone Esc has the production parser's 50ms ambiguity window, followed
// by a render frame. Observe that state before sending the next chord.
const wait = () => new Promise(resolve => setTimeout(resolve, 90));
const oid = (name: string) => name.repeat(40);
function data() {
  return {schema_version: 1, project: "test", snapshot_id: "items", source: "governance_snapshot", total: 0, items: [] as any[], next_cursor: null};
}
function tree() {
  return {schema_version: 1, snapshot_id: "head", head_oid: oid("a"), commits: [
    {oid: oid("a"), parents: [oid("b")], subject: "iteration two"},
    {oid: oid("b"), parents: [oid("c")], subject: "iteration one"}], formal_commits: [] as any[], formal_total: 0, next_cursor: null};
}
function mount(records = data(), workspace = tree(), initialView?: unknown) {
  const previousColor = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode: () => stdin, ref: () => stdin, unref: () => stdin});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: true, columns: 110, rows: 34}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  const calls: {name: string; args: any}[] = [], local = createPublishStateStore().scope("test"); let footer: any;
  const configuration: any = {commit_format: {prefix: "TEST", number_start: 1}, privacy: {}, remote_url: ""};
  if (initialView) local.set("workbench.view", initialView);
  const client: any = {async callTool(name: string, args: any) {
    calls.push({name, args});
    if (name === "publish.items") return records;
    if (name === "publish.workspace") return workspace;
    if (name === "publish.get") return structuredClone(configuration);
    if (name === "publish.set") {configuration[args.section] = structuredClone(args.value); return {ok: true};}
    if (name === "publish.prepare") return {request_id: args.request_id, operation_id: "prepared", effect_state: "committed"};
    if (name === "publish.account.save") return {request_id: args.request_id, operation_id: "account-settings", effect_state: "committed"};
    throw Error("Unexpected API call: " + name);
  }};
  function Fixture() {
    const input = useTextInput();
    return <LanguageContext.Provider value="zh"><Box width="100%" height={stdout.rows} flexDirection="column"><PublishWorkspacePanel client={client} project="test" cmdInput={input} localState={local}
      onFooter={value => {footer = value;}} onDismiss={() => {}} onOpenTrial={() => {}} onOpenFormal={() => {}}/></Box></LanguageContext.Provider>;
  }
  const root = renderSync(<InputProvider><Fixture/></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {calls, local, footer: () => footer,
    async send(value: string) {stdin.write(value); await wait();},
    async repeat(value: string, count: number) {for (let index = 0; index < count; index++) {stdin.write(value); await new Promise(resolve => setTimeout(resolve, 10));} await wait();},
    async resize(w: number, h: number) {stdout.columns = w; stdout.rows = h; stdout.emit("resize"); await wait();},
    text() {const screen = (instances.get(stdout) as any).frontFrame.screen; return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || "").join("")).join("\n");},
    selected() {return selectedRows(instances.get(stdout));},headerFocused(row=0){return focusedHeader(instances.get(stdout),row);},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previousColor;},
  };
}

test("four terminal pages browse real data with no idle editor, and held Down remains bounded", async () => {
  const io = mount();
  try {
    await wait(); expect(io.text()).toContain("Workspace"); expect(io.text()).toContain("版本发布"); expect(io.footer().hidden).toBe(true);
    await io.send("\r"); expect(io.footer().hidden).toBe(true); expect(io.text()).toContain("iteration two");
    for (let index = 0; index < 25; index++) await io.send("\x1b[B");
    expect(io.selected().some(row => row.includes("进入协作"))).toBe(true); expect(io.text()).not.toContain("› "); expect(io.calls.map(c => c.name)).toEqual(["publish.items", "publish.workspace"]);
    for (const width of [40, 80, 110]) {await io.resize(width, 24); expect(io.footer().hidden).toBe(true); expect(io.text()).toContain("进入协作");}
    await io.send("\x1b"); expect(io.text()).not.toContain("←/→ 页头"); await io.send("\x1b[C"); await io.send("\r");
    expect(io.text()).toContain("我的提交"); expect(io.text()).toContain("收到的 PR"); expect(io.footer().hidden).toBe(true);
  } finally {io.close();}
});

test("Publish /help preserves graph selections and reading help starts no operation",async()=>{
 const io=mount();try{
  await wait();await io.send('\r');await io.send('\x1b[C');
  const saved=io.local.get<any>('workbench.view');const reads=io.calls.length;
  await io.send('/help');await io.send('\r');expect(io.text()).toContain('Publish 使用说明');expect(io.text()).toContain('Workspace');
  expect(io.calls).toHaveLength(reads);await io.send('\x1b');expect(io.text()).toContain('过程 Git');
  expect(io.local.get<any>('workbench.view')?.source).toBe(saved?.source);
  expect(io.calls.some(call=>['publish.prepare','publish.execute','publish.set'].includes(call.name))).toBe(false);
 }finally{io.close();}
});

test('Publish primary and secondary headers require Enter; Down and Tab never transfer focus',async()=>{
 const io=mount();try{await wait();expect(io.headerFocused()).toBe(true);await io.send('\x1b[B');await io.send('\t');expect(io.headerFocused()).toBe(true);
  await io.send('\x1b[C');await io.send('\r');expect(io.headerFocused()).toBe(false);expect(io.headerFocused(2)).toBe(true);
  await io.send('\x1b[B');await io.send('\t');expect(io.headerFocused(2)).toBe(true);await io.send('\r');expect(io.headerFocused(2)).toBe(false);
 }finally{io.close();}
});

test("Workbench command discovery keeps the graph and only opens the operation after explicit submission",async()=>{
 const io=mount();try{
  await wait();await io.send('\r');const reads=io.calls.length;
  await io.send('/');expect(io.footer().suggestions.some((row:any)=>row.label==='/formal')).toBe(true);
  expect(io.text()).toContain('过程 Git');expect(io.calls).toHaveLength(reads);
  await io.send('formal');expect(io.calls).toHaveLength(reads);await io.send('\r');
  expect(io.text()).toContain('准备本地正式整理');expect(io.calls.some(call=>call.name==='publish.prepare')).toBe(false);
 }finally{io.close();}
});

test("source graph selection pre-fills the formal draft, while Enter never creates a commit", async () => {
  const io = mount();
  try {
    await wait(); await io.send("\r"); await io.send(" "); await io.send("\x1b[C"); await io.send(" "); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("准备本地正式整理"); expect(io.text()).toContain("publish/formal"); expect(io.footer().hidden).toBe(true);
    expect(io.calls.every(call => ["publish.items", "publish.workspace"].includes(call.name))).toBe(true);
    await io.send("\r"); expect(io.footer().kind).toBe("normal");
    await io.send("feat: coherent change"); await io.send("\r"); expect(io.footer().hidden).toBe(true);
    await io.repeat("\x13", 3);
    const args = io.calls.find(call => call.name === "publish.prepare")!.args;
    expect(io.calls.filter(call => call.name === "publish.prepare")).toHaveLength(1);
    expect(args.target_role).toBe("workspace"); expect(args.selected_commits).toEqual([oid("a"), oid("b")]); expect(args.base_oid).toBe(oid("c"));
    expect(args.title).toBe("feat: coherent change"); expect(args.destination).toBe("formal");
  } finally {io.close();}
});

test("Publish form help retains staged fields and treats /help as literal text while editing",async()=>{
 const io=mount(data(),tree(),{tab:3});try{
  await wait();await io.send('\r');await io.send('\x1b[C');await io.send('\x1b[C');await io.send('\r');await io.send('\r');
  await io.send('\x15');await io.send('/help literal prefix');expect(io.footer().kind).toBe('normal');
  await io.send('\r');const reads=io.calls.length;await io.send('/help');await io.send('\r');
  expect(io.text()).toContain('Publish 使用说明');expect(io.calls).toHaveLength(reads);
  await io.send('\x1b');expect(io.text()).toContain('/help literal prefix');
  expect(io.calls.some(call=>call.name==='publish.set')).toBe(false);await io.send('\x13');
  expect(io.calls.find(call=>call.name==='publish.set')?.args.value.prefix).toBe('/help literal prefix');
 }finally{io.close();}
});

test("saved source selections are never silently reduced when they move outside the visible page", async () => {
  const io = mount(data(), tree(), {tab: 0, source: oid("a"), marked: [oid("f")]});
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("请先核对或清空");
    expect(io.calls.some(call => call.name === "publish.prepare")).toBe(false);
  } finally {io.close();}
});

test("a project with no candidates can open repository PR browsing without preparing a candidate", async () => {
  const io = mount();
  try {
    await wait(); await io.send("\x1b[C"); await io.send("\r"); await io.send("\x1b[C"); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("读取对象"); expect(io.text()).toContain("pulls"); expect(io.footer().hidden).toBe(true);
    expect(io.calls.map(call => call.name)).toEqual(["publish.items", "publish.workspace"]);
  } finally {io.close();}
});

test("configuration sections show their fields immediately, without a third menu or lower separator", async () => {
  const io = mount(data(), tree(), {tab: 3});
  try {
    await wait(); await io.send("\r");
    expect(io.text()).toContain("远端仓库:");
    expect(io.text()).not.toContain("Enter 进入字段列表");
    expect(io.text().split("\n").filter(line => line.trim().startsWith("─"))).toHaveLength(1);
    expect(io.footer().hidden).toBe(true);
    await io.send("\x1b[C");
    expect(io.text()).toMatch(/实例地址:\s+https:\/\/github.com/);
    expect(io.text()).toMatch(/访问令牌:\s+—/);
    expect(io.text()).not.toContain("配置仓库账户");
    await io.send("\x1b[C");
    expect(io.text()).toMatch(/前缀:\s+TEST/); expect(io.footer().hidden).toBe(true);
    expect(io.local.get("settings/group/2")).toBeUndefined();
    await io.send("ignored"); expect(io.text()).toMatch(/前缀:\s+TEST/);
    await io.send("\r"); expect(io.selected().some(row => /前缀:\s+TEST/.test(row))).toBe(true); expect(io.footer().hidden).toBe(true);
    await io.send("\r"); expect(io.footer().kind).toBe("normal");
    await io.send("\x15"); await io.send("DEMO"); await io.send("\r");
    expect(io.text()).toMatch(/前缀:\s+DEMO/); expect(io.footer().hidden).toBe(true);
    expect(io.local.get<any>("settings/group/2").values.prefix).toBe("DEMO");
    await io.send("\x1b"); expect(io.text()).toMatch(/前缀:\s+DEMO/);
    await io.send("\x1b[C"); await io.send("\x1b[D");
    expect(io.text()).toMatch(/前缀:\s+DEMO/);
    expect(io.calls.every(call => ["publish.items", "publish.workspace", "publish.get"].includes(call.name))).toBe(true);
  } finally {io.close();}
});

test("the inline account form masks its token and only Ctrl+S saves through the existing native account API", async () => {
  const io = mount(data(), tree(), {tab: 3});
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[C");
    expect(io.text()).toContain("实例地址:"); expect(io.footer().hidden).toBe(true);
    await io.send("\r");
    await io.send("\x1b[B"); await io.send("\x1b[B"); await io.send("\r");
    await io.send("demo-team/bookmarks"); await io.send("\r");
    await io.send("\x1b[B"); await io.send("\r"); await io.send("demo-maintainer"); await io.send("\r");
    await io.send("\x1b[B"); await io.send("\r"); await io.send("local-example-token");
    expect(io.text()).not.toContain("local-example-token");
    expect(io.footer().cmdInput.value).toBe("•".repeat(19));
    expect(io.local.get<any>("settings/account").values).not.toHaveProperty("token");
    await io.send("\r"); expect(io.calls.some(call => call.name === "publish.account.save")).toBe(false);
    await io.send("\x13");
    expect(io.calls.find(call => call.name === "publish.account.save")!.args).toMatchObject({project: "test", token: "local-example-token",
      target: {kind: "github", instance: "https://github.com", repository: "demo-team/bookmarks", account: "demo-maintainer"}});
    expect(io.footer().hidden).toBe(true); expect(io.text()).toContain("凭据已保存");
  } finally {io.close();}
});

test("a saved configuration rebinds its acknowledged revision before the next edit", async () => {
  const io = mount(data(), tree(), {tab: 3});
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[C"); await io.send("\x1b[C");
    for (const prefix of ["FIRST", "SECOND"]) {
      await io.send("\r"); await io.send("\r"); await io.send("\x15"); await io.send(prefix); await io.send("\x13");
      expect(io.text()).not.toContain("配置已变化");
      expect(io.footer().hidden).toBe(true);
    }
    const writes = io.calls.filter(call => call.name === "publish.set");
    expect(writes).toHaveLength(2);
    expect(writes[0]!.args.expected_value.prefix).toBe("TEST");
    expect(writes[1]!.args.expected_value.prefix).toBe("FIRST");
    expect(writes[1]!.args.value.prefix).toBe("SECOND");
  } finally {io.close();}
});

test("a Workspace validation warning cannot follow navigation or return with the old page", async () => {
  const workspace = tree(); workspace.commits = [{oid: oid("a"), parents: [], subject: "root commit"}];
  const io = mount(data(), workspace);
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("整理范围需要父提交作为基线");
    await io.send("\x1b"); await io.send("\x1b[C");
    expect(io.text()).not.toContain("整理范围需要父提交作为基线");
    await io.send("\x1b[D"); expect(io.text()).not.toContain("整理范围需要父提交作为基线");
    expect(io.calls.some(call => call.name === "publish.prepare")).toBe(false);
  } finally {io.close();}
});

test("Workspace validation feedback expires while staying on the same page", async () => {
  const workspace = tree(); workspace.commits = [{oid: oid("a"), parents: [], subject: "root commit"}];
  const io = mount(data(), workspace);
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("整理范围需要父提交作为基线");
    await new Promise(resolve => setTimeout(resolve, 6150));
    expect(io.text()).not.toContain("整理范围需要父提交作为基线");
    expect(io.calls.some(call => call.name === "publish.prepare")).toBe(false);
  } finally {io.close();}
}, 10000);

test("incoming PR drill-down uses its platform patch rather than the unrelated candidate Git", async () => {
  const records = data(); records.items.push({id: "item", identity_kind: "stable_item", title: "local item", kind: "candidate", scope: "local", owner: "owner", status: "prepared", current_node: "verify",
    reason: "FIXED_CANDIDATE", allowed_actions: ["collaborate", "work"], nodes: [], recorded: {is_incoming: true}, candidate_oid: oid("a"), binding: "binding", revision: 1,
    source: {created_at: ""}, collaboration: {drafts: [], snapshots: [{id: "pr9", binding: "binding", target: {kind: "github", instance: "https://github.com", repository: "example/repo", account: "owner"}, resource: {kind: "pull", number: 9}, page: 1,
      document: {id: "9", title: "Pinned PR nine", refs: {head_sha: oid("d"), base_sha: oid("c")}}, files: [{path: "feature.ts", old_path: "feature.ts", unavailable: false, patch: "@@ -1 +1 @@\n-old\n+saved PR patch\n"}]}]}});
  const io = mount(records);
  try {
    await wait(); await io.send("\x1b[C"); await io.send("\r"); await io.send("\x1b[C"); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("查看变更"); expect(io.text()).toContain("讨论与记录"); expect(io.text()).toContain("处理这个 PR");
    await io.send("\r"); await io.send("\r"); expect(io.text()).toContain("saved PR patch");
    expect(io.calls.some(call => call.name === "publish.diff")).toBe(false); expect(io.footer().hidden).toBe(true);
    await io.send("\x1b"); await io.send("\x1b"); await io.send("\x1b[B"); await io.send("\x1b[B"); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("来源尚未绑定"); expect(io.calls.some(call => call.name === "publish.prepare")).toBe(false);
  } finally {io.close();}
});
