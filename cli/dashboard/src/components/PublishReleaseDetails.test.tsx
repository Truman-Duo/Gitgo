import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import chalk from "chalk";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {selectedRows} from "../testing/selectedRows.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {ModuleHelpBoundary} from "./ModuleHelpBoundary.js";
import {PublishReleaseDetails} from "./PublishReleaseDetails.js";
import {PublishWorkspacePanel} from "./PublishWorkspacePanel.js";
import {createPublishStateStore} from "../state/publishLocalState.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {colors} from "../theme/index.js";

const wait = (ms = 90) => new Promise(resolve => setTimeout(resolve, ms));
function fixture(kind = "github", planned = true) {
  const target = {kind, instance: `https://${kind}.com`, repository: "team/demo", account: "maintainer"};
  const control: any = {schema_version: 1, plan: {id: "plan", binding: "fixed-plan", version: "2.0", package_id: "package",
    target, supported_phases: kind === "github" ? ["prepared", "draft", "published"] : ["prepared", "published"], capabilities: {}}, history: []};
  const item: any = {id: "item", identity_kind: "stable_item", status: "integrated", title: "Fixed item", revision: 10, binding: "source",
    target: "refs/heads/main", allowed_actions: ["release_plan", "release_run"], packages: [
      {id: "package", version: "2.0", filename: "source.zip", source_oid: "a".repeat(40), tree_oid: "b".repeat(40),
        sha256: "c".repeat(64), assets: [{name: "source.zip", sha256: "d".repeat(64), size: 42}]},
    ], ...(planned ? {release_controller: control} : {}), collaboration: {snapshots: [{id: "observation", binding: "source", target,
      resource: {kind: "pulls"}, documents: []}], drafts: []}};
  return {item, control, target};
}
function mount(options: {kind?: string; planned?: boolean; queued?: boolean; statusError?: boolean; workbench?: boolean; ci?: boolean;native?:boolean;savedCI?:boolean;metrics?:boolean; language?: "en" | "zh"} = {}) {
  const previousColor = chalk.level; chalk.level = 3;
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode: () => stdin, ref: () => stdin, unref: () => stdin});
  const stdout: any = new PassThrough(); let output = ""; Object.assign(stdout, {isTTY: true, columns: 100, rows: 32}); stdout.on("data", (value: any) => {output += String(value);});
  const stderr: any = new PassThrough(); let errors = ""; stderr.on("data", (value: any) => {errors += String(value);});
  const {item, control, target} = fixture(options.kind, options.planned), local = createPublishStateStore().scope("test");
  if(options.ci||options.native){control.plan.capabilities={ci_acquisition:true,anonymous_delivery_verification:true,native_update_recipe:Boolean(options.native)};control.plan.supported_phases.push("verified");}
  if (options.queued) {
    control.job = {id: "previous-job", state: "queued", through: "published", phases: {}, record_count: 1, created_at: "now"};
    item.allowed_actions = ["release_cancel", "release_reconcile"];
  }
  local.set("workbench.view", {tab: 2, sub: 1});
  const calls: {name: string; args: any}[] = [], receipts = new Map<string, any>();
  let footer: any, dismissed = 0, loseResponse = false, statusError = options.statusError, heldRead: (() => Promise<any>) | null = null,heldSources:(()=>Promise<any>)|null=null;
  const client: any = {async callTool(name: string, args: any) {
    calls.push({name, args: structuredClone(args)});
    if (name === "publish.items") return {schema_version: 1, project: "test", snapshot_id: "items", items: [structuredClone(item)], total: 1, next_cursor: null};
    if (name === "publish.workspace") return {schema_version: 1, snapshot_id: "head", commits: [], formal_commits: [], formal_total: 0, next_cursor: null};
    if (name === "publish.item") return heldRead ? heldRead() : {item: structuredClone(item)};
    if (name === "publish.release.status") {
      if (statusError) throw Error("Saved plan unavailable");
      const events = control.job ? [{sequence: 1, name: "run", state: control.job.state, time: 1}] : [];
      if(options.metrics)events.push({sequence:2,name:"check-40",state:"success",time:2,
        result:{timing:{queue_seconds:30,observed_job_span_seconds:60,runner_occupied_seconds:120}}} as any);
      return {item_id: item.id, revision: item.revision, recorded_only: true, stale: false, control: structuredClone(control),
        plan: {target, body: "Reviewed notes", notes:"Reviewed notes", source_oid:"a".repeat(40),ci_artifacts:options.savedCI?
          [{artifact_id:99,run_id:9,attempt:1,name:"old-artifact",workflow:"build.yml",size:42,sha256:"f".repeat(64)}]:[],
          recipe: {id: "project.fixed-package", version: 1}, supported_phases: control.plan.supported_phases, capabilities: control.plan.capabilities},
        events, next_sequence: events.length, has_more: false};
    }
    if(name==="publish.release.sources")return heldSources?heldSources():{item_id:item.id,plan_binding:control.plan.binding,source_oid:"a".repeat(40),
      remote_writes:false,page:args.page,has_more:false,rows:[{artifact_id:40,run_id:10,attempt:2,name:"native-build",workflow:"build.yml",size:123,sha256:"d".repeat(64)}]};
    if (receipts.has(args.request_id)) return structuredClone(receipts.get(args.request_id));
    if (name === "publish.release.plan") {item.release_controller = control; item.revision++;}
    else if(name==="publish.signing.save")return {request_id:args.request_id,operation_id:"signing-settings",effect_state:"committed"};
    else if (["publish.release.run", "publish.release.reconcile"].includes(name)) {
      control.job = {id: "job", state: "queued", through: args.through ?? "published", observe_only: name.endsWith("reconcile"),
        phases: {}, record_count: 0, created_at: "now"};
      item.allowed_actions = ["release_cancel", "release_reconcile"]; item.revision++;
    } else if (name === "publish.release.cancel") {control.job.state = "stopping"; item.revision++;}
    else throw Error("Unexpected API call: " + name);
    const result = {request_id: args.request_id, operation_id: "operation", effect_state: "committed", item: structuredClone(item)};
    receipts.set(args.request_id, result);
    if (loseResponse) {loseResponse = false; throw Error("Response lost after admission");}
    return result;
  }};
  function Fixture() {
    const input = useTextInput();
    const onFooter = React.useCallback((value: any) => {footer = value;}, []);
    return <LanguageContext.Provider value={options.language ?? "zh"}><Box width="100%" height={stdout.rows} flexDirection="column">
      {options.workbench ? <PublishWorkspacePanel client={client} project="test" localState={local} cmdInput={input} onFooter={onFooter}
        onDismiss={() => dismissed++} onOpenTrial={() => {}} onOpenFormal={() => {}}/>
        : <ModuleHelpBoundary onFooter={onFooter}>{footerOwner => <PublishReleaseDetails client={client} project="test" initialItem={item}
          packageId="package" localState={local} onFooter={footerOwner} onDismiss={() => dismissed++}/>}</ModuleHelpBoundary>}
    </Box></LanguageContext.Provider>;
  }
  const root = renderSync(<InputProvider><Fixture/></InputProvider>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const selected = () => selectedRows(instances.get(stdout));
  const send = async (value: string) => {stdin.write(value); await wait();};
  return {item, control, calls, local, receipts, dismissed: () => dismissed, footer: () => footer, selected, send,
    loseNextResponse() {loseResponse = true;}, holdReads(value: () => Promise<any>) {heldRead = value;},
    holdSources(value:()=>Promise<any>){heldSources=value;},
    restoreStatus() {statusError = false;},
    async choose(label: string) {
      for (let index = 0; index < 16; index++) {if (selected().some(line => line.includes(label))) return; const before = selected().join(); await send("\x1b[A"); if (selected().join() === before) break;}
      for (let index = 0; index < 16; index++) {if (selected().some(line => line.includes(label))) return; await send("\x1b[B");}
      throw Error("No selectable row: " + label + "\n" + selected().join("\n"));
    },
    text() {const instance = instances.get(stdout) as any; if (!instance) throw Error(errors || output.slice(-6500) || "Renderer exited"); const screen = instance.frontFrame.screen; return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || "").join("")).join("\n");},
    async resize(w: number, h: number) {stdout.columns = w; stdout.rows = h; stdout.emit("resize"); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); chalk.level = previousColor;},
  };
}

test("existing version opens its fixed package and only reads saved facts", async () => {
  const io = mount({workbench: true});
  try {
    await wait();await io.send("\x1b[C");await io.send("\x1b[C");await io.send("\r");await io.send("\x1b[C");await io.send("\r");await io.send("\r");
    expect(io.text()).toContain("版本 · 2.0"); expect(io.text()).toContain("team/demo");
    expect(io.calls.filter(call => call.name === "publish.item").at(-1)?.args.item_id).toBe("item");
    expect(io.calls.every(call => ["publish.items", "publish.workspace", "publish.item", "publish.release.status"].includes(call.name))).toBe(true);
    await io.send("\x1b"); expect(io.text()).toContain("已有版本");
    expect(io.calls.some(call => call.name === "publish.release.cancel")).toBe(false);
  } finally {io.close();}
}, 10000);

test("a release plan fixes the selected package through the shared staged form", async () => {
  const io = mount({planned: false});
  try {
    await wait(); await io.send("\r"); expect(io.text()).toContain("发布计划");
    expect(io.text()).not.toContain("产物记录 ID"); expect(io.footer().hidden).toBe(true);
    await io.send("\r"); await io.send("\r"); expect(io.calls.some(call => call.name === "publish.release.plan")).toBe(false);
    await io.send("\x13");
    expect(io.calls.filter(call => call.name === "publish.release.plan")).toHaveLength(1);
    expect(io.calls.find(call => call.name === "publish.release.plan")!.args).toMatchObject({package_id: "package", expected_revision: 10,
      target: {kind: "github", repository: "team/demo", account: "maintainer"}});
    expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
  } finally {io.close();}
});

test("CI artifacts use the existing selection scene and save exact source identities without starting a release", async()=>{
  const io=mount({ci:true});
  try{
    await wait();expect(io.calls.some(call=>call.name==="publish.release.sources")).toBe(false);
    await io.choose("选择 CI 产物");await io.send("\r");
    expect(io.text()).toContain("native-build");expect(io.calls.filter(call=>call.name==="publish.release.sources")).toHaveLength(1);
    await io.send("\r");await io.send("\x13");
    expect(io.calls.filter(call=>call.name==="publish.release.plan")).toHaveLength(1);
    expect(io.calls.find(call=>call.name==="publish.release.plan")!.args).toMatchObject({package_id:"package",notes:"Reviewed notes",
      ci_artifacts:[{run_id:10,attempt:2,artifact_id:40}]});
    expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
    expect(io.text()).toContain("版本 ·");
    await io.choose("交付核对");await io.send("\r");await io.choose("核对匿名交付");await io.send("\r");
    expect(io.text()).toContain("完成尚未完成的发布阶段");
    await io.send("\x1b");expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
},10000);

test("project signing secrets stay masked and outside local drafts; localized recipe saves the stable identity",async()=>{
  const io=mount({native:true});
  try{
    await wait();await io.choose("配置项目签名密钥");await io.send("\r");
    await io.send("\r");await io.send("test-publisher");await io.send("\r");await io.send("\x1b[B");await io.send("\r");
    const secret="A".repeat(43)+"=";await io.send(secret);
    expect(io.text()).not.toContain(secret);expect(io.footer().cmdInput.value).toBe("•".repeat(secret.length));
    expect((io.local.get<any>("release-signing/item/package") as any)?.values?.token).toBeUndefined();
    await io.send("\x13");
    const saved=io.calls.find(call=>call.name==="publish.signing.save")!.args;
    expect(saved).toMatchObject({key_id:"test-publisher",token:secret,target:{repository:"team/demo",account:"maintainer"}});
    expect(saved.item_id).toBeUndefined();expect(saved.ci_artifacts).toBeUndefined();
    expect(io.local.get("release-signing/item/package")).toBeUndefined();
    await io.choose("编辑发布计划");await io.send("\r");await io.send("\x1b[F");await io.send("\x1b[A");
    await io.send("\r");await io.send("\x1b[C");await io.send("\r");
    expect(io.text()).toContain("Gitgo 原生更新");expect(io.text()).toContain("test-publisher");
    await io.send("\x13");
    expect(io.calls.find(call=>call.name==="publish.release.plan")!.args).toMatchObject({delivery_recipe:"gitgo.native",signing_key_id:"test-publisher",upgrade_route:"direct"});
    expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
},10000);

test("leaving CI selection discards late source reads and keeps the version detail",async()=>{
  const io=mount({ci:true});let resolve:(value:any)=>void=()=>{};
  try{
    await wait();io.holdSources(()=>new Promise(done=>{resolve=done;}));
    await io.choose("选择 CI 产物");await io.send("\r");await io.send("\x1b");
    resolve({item_id:"item",plan_binding:"fixed-plan",source_oid:"a".repeat(40),remote_writes:false,page:1,has_more:false,
      rows:[{artifact_id:99,name:"late-source"}]});await wait();
    expect(io.text()).toContain("版本 ·");expect(io.text()).not.toContain("late-source");expect(io.dismissed()).toBe(0);
    expect(io.calls.some(call=>call.name==="publish.release.plan"||call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
});

test("a saved artifact absent from the current CI observation remains removable",async()=>{
  const io=mount({ci:true,savedCI:true});
  try{
    await wait();await io.choose("选择 CI 产物");await io.send("\r");await io.choose("old-artifact");
    expect(io.text()).toContain("未出现在本次观察中");await io.send("\r");await io.send("\x13");
    expect(io.calls.find(call=>call.name==="publish.release.plan")!.args.ci_artifacts).toEqual([]);
    expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
});

test("transfer throughput and task/CI timing distinguish wall time, queue and parallel occupancy",async()=>{
  const io=mount({queued:true,metrics:true});
  try{
    io.control.job.state="completed";io.control.job.elapsed_seconds=60;io.control.job.queue_seconds=5;
    io.control.job.transfer={direction:"download",asset:"package.zip",bytes:2000,total:2000,seconds:2,state:"completed"};
    await wait();await io.choose("刷新已保存状态");await io.send("\r");
    expect(io.text()).toContain("任务耗时 60.00 s");expect(io.text()).toContain("排队 5.00 s");
    expect(io.text()).toContain("平均吞吐 1000 B/s");expect(io.text()).toContain("传输耗时 2.00 s");
    await io.send("\x1b[6~");expect(io.text()).toContain("CI 观察区间 60.00 s");expect(io.text()).toContain("Runner 占用 120.00 s");
    expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
});

test("read-only phase observations are distinct from a fresh execution pass",async()=>{
  const io=mount({queued:true});
  try{
    io.control.job.state="completed";io.control.job.observe_only=true;
    io.control.job.phases={prepared:{state:"observed"},draft:{state:"observed"}};
    await wait();await io.choose("刷新已保存状态");await io.send("\r");
    expect(io.text()).toContain("仅对账观察");expect(io.text()).toContain("仅核对观察");
    expect(io.calls.some(call=>call.name==="publish.release.run")).toBe(false);
  }finally{io.close();}
});

test("draft and publication require explicit confirmation; admission only reports queued", async () => {
  const io = mount();
  try {
    await wait(); await io.choose("远端草稿"); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("确认执行");
    await io.send("\r"); expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
    await io.send("\r"); await io.send("\x1b[C");
    io.loseNextResponse(); await io.send("\r");
    expect(io.text()).toContain("排队中"); expect(io.text()).not.toContain("所选阶段已完成");
    await io.choose("重读同一请求"); await io.send("\r");
    const runs = io.calls.filter(call => call.name === "publish.release.run"); expect(runs).toHaveLength(2);
    expect(runs[0]!.args).toEqual(runs[1]!.args); expect(runs[0]!.args.through).toBe("draft"); expect(io.receipts.size).toBe(1);
    await io.choose("停止本次发布"); await io.send("\r");
    expect(io.calls.find(call => call.name === "publish.release.cancel")!.args.job_id).toBe("job");
    expect(io.text()).toContain("正在停止");
  } finally {io.close();}
});

test("GitLab exposes its actual phases, and help/resize do not start release work", async () => {
  const io = mount({kind: "gitlab"});
  try {
    await wait(); expect(io.text()).toContain("正式发布"); expect(io.text()).not.toContain("远端草稿 ·");
    const before = io.calls.length; await io.send("/help"); await io.send("\r"); expect(io.text()).toContain("版本发布");
    expect(io.calls).toHaveLength(before); await io.send("\x1b");
    await io.choose("正式发布"); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    await io.resize(42, 12); await io.send("\x1b"); await io.resize(100, 32);
    expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
    expect(io.selected().some(line => line.includes("发布这个版本"))).toBe(true);
  } finally {io.close();}
});

test("publication submits the exact fixed plan revision after an explicit confirmation", async () => {
  const io = mount({language: "en"});
  try {
    await wait(); await io.choose("Publication"); await io.send("\r"); await io.send("\x1b[B"); await io.send("\r");
    expect(io.text()).toContain("team/demo"); expect(io.text()).toContain("maintainer");
    await io.send("\x1b[C"); await io.send("\r");
    expect(io.calls.filter(call => call.name === "publish.release.run")).toHaveLength(1);
    expect(io.calls.find(call => call.name === "publish.release.run")!.args).toMatchObject({item_id: "item", expected_revision: 10, through: "published"});
    expect(io.text()).toContain("Queued"); expect(io.text()).not.toContain("Requested phases completed");
  } finally {io.close();}
});

test("re-entering a queued task observes it without replay and cancellation uses exact job identity", async () => {
  const io = mount({queued: true});
  try {
    await wait(); expect(io.text()).toContain("排队中");
    expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
    await io.choose("停止本次发布"); await io.send("\r");
    expect(io.calls.find(call => call.name === "publish.release.cancel")!.args.job_id).toBe("previous-job");
  } finally {io.close();}
});

test("unmounted reads cannot publish a late result or start a worker", async () => {
  const io = mount({language: "en"});
  await wait(); let release: (value: any) => void = () => {};
  io.holdReads(() => new Promise(resolve => {release = resolve;}));
  await io.choose("Refresh saved state"); await io.send("\r"); const count = io.calls.length;
  io.close(); release({item: io.item}); await wait();
  expect(io.calls).toHaveLength(count); expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
});

test("an unavailable plan preview cannot enable publication and a fresh read restores the exact plan", async () => {
  const previous = colors.feedback.errorMs; Object.assign(colors.feedback, {errorMs: 600});
  const io = mount({statusError: true});
  try {
    await wait(); await io.choose("正式发布"); await io.send("\r");
    expect(io.text()).toContain("Saved plan unavailable"); expect(io.text()).not.toContain("发布这个版本…");
    await new Promise(resolve => setTimeout(resolve, 650));
    expect(io.text()).not.toContain("Saved plan unavailable"); expect(io.text()).toContain("计划详情尚未读取");
    expect(io.text()).not.toContain("发布这个版本…");
    await io.send("\r"); expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
    io.restoreStatus(); await io.choose("刷新已保存状态"); await io.send("\r");
    await io.choose("正式发布"); await io.send("\r"); await io.choose("发布这个版本");
    expect(io.text()).toContain("发布这个版本…");
    expect(io.text()).not.toContain("Saved plan unavailable");
    expect(io.calls.some(call => call.name === "publish.release.run")).toBe(false);
  } finally {io.close(); Object.assign(colors.feedback, {errorMs: previous});}
});
