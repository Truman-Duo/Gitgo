import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Box, renderSync} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {LanguageContext} from "../ui/PageFrame.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {PublishWorkspacePanel} from "./PublishWorkspacePanel.js";
import {createPublishStateStore, type PublishLocalState} from "../state/publishLocalState.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 80));

test("platform search pagination preserves its repository query and does not send a draft", async () => {
  const calls: {operation: string; args: any}[] = [];
  const client: any = {async callTool(operation: string, args: any) {
    const value = candidate(args.project);
    Object.assign(value.items[0], {allowed_actions: ["collaborate"], collaboration: {drafts: [], snapshots: [{
      id: "search-fixed", binding: "current", target: {kind: "github", instance: "https://github.com", repository: "team/repo", account: "alice"},
      resource: {kind: "issues"}, page: 1, query: "release fix", documents: [], more: true}]}});
    if (operation === "publish.items") return value;
    calls.push({operation, args});
    return {request_id: args.request_id, operation_id: "next", effect_state: "committed", item: value.items[0]};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("platform-more"); await io.send("\r");
    expect(calls).toHaveLength(1);
    expect(calls[0].operation).toBe("publish.collaborate");
    expect(calls[0].args).toMatchObject({action: "search", query: "release fix", page: 2, item_id: "fixed-item", expected_revision: 1});
    expect(calls[0].args.target.repository).toBe("team/repo");
  } finally { io.close(); }
});

test("closing a platform issue creates a fixed-snapshot draft requiring a separate send", async () => {
  const calls: {operation: string; args: any}[] = [];
  const client: any = {async callTool(operation: string, args: any) {
    const value = candidate(args.project);
    Object.assign(value.items[0], {allowed_actions: ["collaborate"], collaboration: {drafts: [], snapshots: [{
      id: "issue-observed", binding: "current", target: {kind: "github", instance: "https://github.com", repository: "team/repo", account: "alice"},
      resource: {kind: "issue", number: 7}, page: 1, document: {id: "7", title: "Fixed issue", state: "open", body: "old"}}]}});
    if (operation === "publish.items") return value;
    calls.push({operation, args});
    return {request_id: args.request_id, operation_id: "draft", effect_state: "committed", item: value.items[0]};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("state"); await io.send("\r");
    expect(calls).toHaveLength(0); await io.send("\r");
    expect(calls).toHaveLength(1); expect(calls[0].operation).toBe("publish.collaborate");
    expect(calls[0].args).toMatchObject({action: "draft", snapshot_id: "issue-observed", draft: {kind: "state", state: "closed", body: ""}});
  } finally { io.close(); }
});
function snapshot(project: string, title = "候选 🚀", count = 3) {
  return {schema_version: 1, project, snapshot_id: project, total: count, next_cursor: null,
    items: Array.from({length: count}, (_, index) => ({
      id: `${project}-${index}`, title: `${title} ${index}`, label: `TEST-${index}`, status: "needs_reconciliation",
      current_node: "verify", recorded: {synced: true, pushed: true}, source: {state_ref: null},
      nodes: [{id: "record", status: "recorded", reason: "LEGACY_RECORD_ONLY"},
        {id: "verify", status: "needs_reconciliation", reason: "LEGACY_CANDIDATE_UNBOUND"},
        {id: "integrate", status: "unverified", reason: "RESULT_OID_UNRECORDED"},
        {id: "push", status: "unverified", reason: "REMOTE_RESULT_UNVERIFIED"}],
    }))};
}

function candidate(project: string, status = "prepared", revision = 1) {
  const value = snapshot(project, "待接手贡献", 1);
  Object.assign(value.items[0], {id: "fixed-item", identity_kind: "stable_item", kind: "candidate", revision,
    status, binding: "current", reason: status === "adopted" ? "ADOPTED_REWORK_REQUIRED" : "CHECKS_FAILED_ADOPTION_AVAILABLE",
    owner: "maintainer", rework: "修复失败并复评", candidate_oid: "a".repeat(40), target: "refs/heads/publish/work",
    candidate: {role: "workspace", source_ref: "HEAD", oid: "a".repeat(40)},
    evidence: [{id: "check", binding: "current", kind: "git_diff_check", outcome: "failed", summary: "trailing whitespace"}],
    allowed_actions: status === "prepared" ? ["plan", "revise", "adopt"] : ["revise"],
    nodes: [{id: "record", status: "recorded", reason: "FIXED_CANDIDATE"},
      {id: "verify", status: "failed", reason: "BOUNDED_CHECKS_ONLY"},
      {id: "integrate", status, reason: status === "adopted" ? "ADOPTED_REWORK_REQUIRED" : "CHECKS_FAILED_ADOPTION_AVAILABLE"}],
  });
  return value;
}

test("a running verification remains pausable and resume does not run another check", async () => {
  let status = "prepared", revision = 1;
  const calls: {op: string; args: any}[] = [];
  let finish: ((value: any) => void) | undefined;
  let verifyArgs: any;
  const view = (project: string) => {
    const result = candidate(project, status, revision);
    Object.assign(result.items[0], {reason: status === "paused" ? "PUBLISH_PAUSED" : "CHECK_OPERATION_INCOMPLETE",
      allowed_actions: status === "paused" ? ["resume"] : ["verify", "pause", "reconcile"]});
    return result;
  };
  const client: any = {callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return Promise.resolve(view(args.project));
    if (op === "publish.item") return Promise.resolve({item: view(args.project).items[0]});
    if (op === "publish.verify") {
      status = "checking"; revision = 2; verifyArgs = args;
      return new Promise(resolve => { finish = resolve; });
    }
    if (op === "publish.pause") { status = "pausing"; revision = 3; }
    if (op === "publish.resume") { status = "prepared"; revision = 5; }
    return Promise.resolve({request_id: args.request_id, operation_id: "operation", effect_state: "committed", item: view(args.project).items[0]});
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); await wait(); };
  try {
    await wait(); await command("verify");
    expect(io.text()).toContain("可用 /pause 请求暂停");
    await command("pause");
    expect(calls.find(c => c.op === "publish.pause")!.args.expected_revision).toBe(2);
    status = "paused"; revision = 4;
    finish!({request_id: verifyArgs.request_id, operation_id: "check", effect_state: "committed", item: view("one").items[0]});
    await wait(); await command("resume");
    expect(calls.find(c => c.op === "publish.resume")!.args.expected_revision).toBe(4);
    expect(calls.filter(c => c.op === "publish.verify")).toHaveLength(1);
    expect(calls.some(c => c.op === "publish.execute")).toBe(false);
  } finally { io.close(); }
});

test("Formal preparation, verification and integration require separate commands bound to current revisions", async () => {
  const calls: {op: string; args: any}[] = [];
  let phase = 0;
  const formal = (project: string) => {
    const value = candidate(project, phase === 3 ? "integrated" : "prepared", phase === 1 ? 1 : 3);
    const row: any = value.items[0];
    Object.assign(row, {reason: phase === 3 ? "FORMAL_INTEGRATED" : phase === 2 ? "FORMAL_READY" : "FORMAL_TEST_REQUIRED",
      allowed_actions: phase === 1 ? ["verify", "revise"] : phase === 2 ? ["verify", "execute", "revise"] : ["revise"],
      plan: {destination: "formal", method: "squash", repository: "local", target_ref: "refs/heads/release",
        expected_target_oid: "b".repeat(40), tree_oid: "c".repeat(40), test_target: "tests/test_release.py",
        commit_message: "[DEMO-12] feat: release", excluded_path_fingerprints: ["private"]},
      evidence: [{id: "privacy", binding: "current", kind: "outbound_privacy", outcome: "passed", summary: ""}]});
    return value;
  };
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return phase ? formal(args.project) : snapshot(args.project, "", 0);
    phase = op === "publish.prepare" ? 1 : op === "publish.verify" ? 2 : 3;
    return {request_id: args.request_id, operation_id: "operation", effect_state: "committed", item: formal(args.project).items[0]};
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); await wait(); };
  try {
    await wait(); await command("formal");
    expect(io.text()).toContain("准备本地正式整理");
    await io.send("\r"); await io.send("\r");
    await io.send("release"); await io.send("\r"); await io.send("\r");
    await io.send("maintainer"); await io.send("\r");
    await io.send("tests/test_release.py"); await io.send("\r");
    expect(calls.filter(c => c.op !== "publish.items")).toHaveLength(0);
    await io.send("\r"); await wait();
    const prepared = calls.find(c => c.op === "publish.prepare")!;
    expect(prepared.args.destination).toBe("formal"); expect(prepared.args.method).toBe("squash");
    expect(prepared.args.checks).toEqual([{target: "tests/test_release.py", seeds: [42], timeout: 30}]);
    await command("execute");
    expect(calls.some(c => c.op === "publish.execute")).toBe(false);
    await command("verify");
    expect(calls.find(c => c.op === "publish.verify")!.args.expected_revision).toBe(1);
    await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("[DEMO-12] feat: release");
    expect(io.text()).toContain("出站隐私检查");
    await command("execute");
    expect(calls.find(c => c.op === "publish.execute")!.args.expected_revision).toBe(3);
    await io.send("\x1b");
    expect(io.text()).toContain("已本地合入");
  } finally { io.close(); }
});

test("combine and selective methods submit the explicitly chosen inputs", async () => {
  const calls: {op: string; args: any}[] = [];
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return snapshot(args.project, "", 0);
    return {request_id: args.request_id, operation_id: "saved", effect_state: "committed"};
  }};
  const io = mount(client);
  const command = async (value: string) => { await io.send("/"); await io.send(value); await io.send("\r"); };
  try {
    await wait(); await command("combine");
    await io.send("\r"); await io.send("\r");
    await io.send("trial | refs/heads/peer"); await io.send("\r");
    await io.send("formal"); await io.send("\r"); await io.send("\r");
    await io.send("owner"); await io.send("\r"); await io.send("\r"); await io.send("\r");
    await wait();
    expect(calls.find(row => row.op === "publish.prepare")?.args.contributions).toEqual([{source_role: "trial", source_ref: "refs/heads/peer"}]);
    await command("formal"); await io.send("\r"); await io.send("\r");
    await io.send("formal"); await io.send("\r"); await io.send("\x15"); await io.send("partial"); await io.send("\r");
    await io.send("src/a.py; tests/test_a.py"); await io.send("\r");
    await io.send("owner"); await io.send("\r"); await io.send("\r"); await io.send("\r"); await wait();
    expect(calls.filter(row => row.op === "publish.prepare")[1].args.paths).toEqual(["src/a.py", "tests/test_a.py"]);
  } finally { io.close(); }
});

test("conflict handoff fixes the source ID and keeps the item revision", async () => {
  let submitted: any;
  const view = () => {
    const result = candidate("one", "conflicted", 7);
    Object.assign(result.items[0], {allowed_actions: ["revise", "work", "decide"], reason: "PUBLISH_MERGE_CONFLICT",
      candidate: {role: "trial", source_ref: "refs/heads/incoming", oid: "a".repeat(40)},
      plan: {destination: "formal", method: "merge", conflict_paths: ["src/a.py"]}});
    return result;
  };
  const client: any = {async callTool(op: string, args: any) {
    if (op === "publish.items") return view();
    submitted = args;
    return {request_id: args.request_id, operation_id: "work", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("work"); await io.send("\r");
    for (let i = 0; i < 4; i++) await io.send("\r");
    await io.send("resolve conflict"); await io.send("\r"); await wait();
    expect(submitted.destination).toBe("work"); expect(submitted.source_ref).toBe("a".repeat(40));
    expect(submitted.expected_revision).toBe(7); expect(submitted.item_id).toBe("fixed-item");
    expect(submitted.target_branch).toBe("publish/repair");
  } finally { io.close(); }
});

test("preparing uses a draft then sends fixed item revision for adoption and retains failed evidence", async () => {
  const calls: {op: string; args: any}[] = [];
  let prepared = false; let adopted = false;
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return prepared ? candidate(args.project, adopted ? "adopted" : "prepared", adopted ? 3 : 1) : snapshot(args.project, "", 0);
    if (op === "publish.prepare") prepared = true;
    if (op === "publish.adopt") adopted = true;
    return {item: candidate(args.project, adopted ? "adopted" : "prepared").items[0],
      request_id: args.request_id, operation_id: "operation", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("prepare"); await io.send("\r");
    expect(io.footer().kind).toBe("normal");
    for (let i = 0; i < 3; i++) await io.send("\r");
    await io.send("maintainer"); await io.send("\r");
    expect(calls.map(c => c.op)).toEqual(["publish.items"]);
    await io.send("修复失败并复评"); await io.send("\r"); await wait();
    expect(calls[1].args.source_ref).toBe("HEAD"); expect(calls[1].args.owner).toBe("maintainer");
    expect(io.footer().kind).toBe("command");
    await io.send("/"); await io.send("adopt"); await io.send("\r"); await wait();
    const adoptedCall = calls.find(c => c.op === "publish.adopt")!;
    expect(adoptedCall.args.item_id).toBe("fixed-item"); expect(adoptedCall.args.expected_revision).toBe(1);
    expect(io.text()).toContain("已接手");
    await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("Git 空白检查 · 失败"); expect(io.text()).toContain("修复失败并复评");
  } finally { io.close(); }
});

test("an uncertain command retries with the same request identity", async () => {
  const ids: string[] = [];
  const client: any = {async callTool(op: string, args: any) {
    if (op === "publish.items") return candidate(args.project);
    ids.push(args.request_id);
    if (ids.length === 1) throw new Error("transport timeout");
    return {request_id: args.request_id, operation_id: "operation", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("adopt"); await io.send("\r");
    expect(io.text()).toContain("复用原请求");
    await io.send("/"); await io.send("retry"); await io.send("\r");
    expect(ids.length).toBe(2); expect(ids[0]).toBe(ids[1]);
  } finally { io.close(); }
});

test("a rejected draft keeps entered values and permits correction before a new request", async () => {
  const requests: any[] = [];
  const client: any = {async callTool(op: string, args: any) {
    if (op === "publish.items") return snapshot(args.project, "", 0);
    requests.push(args);
    return {request_id: args.request_id, operation_id: "operation", effect_state: requests.length === 1 ? "not_committed" : "committed",
      ...(requests.length === 1 ? {error: "PUBLISH_TARGET_EXISTS"} : {})};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("prepare"); await io.send("\r");
    for (let i = 0; i < 3; i++) await io.send("\r");
    await io.send("maintainer"); await io.send("\r"); await io.send("repair"); await io.send("\r");
    expect(io.footer().kind).toBe("normal"); expect(io.text()).toContain("目标分支已存在");
    await io.send("\x1b[A"); await io.send("\x1b[A"); await io.send("\x15"); await io.send("publish/corrected");
    await io.send("\r"); await io.send("\r"); await io.send("\r");
    expect(requests.length).toBe(2); expect(requests[1].target_branch).toBe("publish/corrected");
    expect(requests[1].owner).toBe("maintainer"); expect(requests[1].rework).toBe("repair");
    expect(requests[1].request_id).not.toBe(requests[0].request_id);
  } finally { io.close(); }
});

test("late mutation results cannot refresh or change another project", async () => {
  let finish: ((value: any) => void) | undefined; let mutation: any;
  const reads: string[] = [];
  const client: any = {callTool(op: string, args: any) {
    if (op === "publish.items") { reads.push(args.project); return Promise.resolve(candidate(args.project)); }
    mutation = args; return new Promise(resolve => { finish = resolve; });
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("adopt"); await io.send("\r");
    await io.project("two");
    finish!({request_id: mutation.request_id, operation_id: "operation", effect_state: "committed"}); await wait();
    expect(reads).toEqual(["one", "two"]); expect(io.text()).not.toContain("已保存，事项");
  } finally { io.close(); }
});

test("a delayed refresh cannot retarget the item revision in an open plan draft", async () => {
  let reads = 0; let finish: ((value: any) => void) | undefined; let saved: any;
  const client: any = {callTool(op: string, args: any) {
    if (op === "publish.items") {
      reads++;
      if (reads === 2) return new Promise(resolve => { finish = resolve; });
      return Promise.resolve(candidate(args.project));
    }
    saved = args;
    return Promise.resolve({request_id: args.request_id, operation_id: "op", effect_state: "committed"});
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("refresh"); await io.send("\r");
    await io.send("/"); await io.send("plan"); await io.send("\r");
    const changed = candidate("one", "prepared", 7); changed.items[0].id = "another-item";
    finish!(changed); await wait();
    await io.send("\r"); await io.send("\r"); await io.send("\r");
    expect(saved.item_id).toBe("fixed-item"); expect(saved.expected_revision).toBe(1);
  } finally { io.close(); }
});
function mount(client: any, initial = "one", localState?: PublishLocalState) {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin; stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = true; stdout.columns = 120; stdout.rows = 32; stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  let footer: any; let dismissed = 0;
  const onFooter = (value: any) => { footer = value; };
  function Fixture({project}: {project: string}) {
    const input = useTextInput();
    return <LanguageContext.Provider value="zh"><Box width="100%" height={stdout.rows} flexDirection="column">
      <PublishWorkspacePanel client={client} project={project} cmdInput={input} onFooter={onFooter}
        localState={localState}
        onDismiss={() => dismissed++} onOpenTrial={() => {}} onOpenFormal={() => {}}/>
    </Box></LanguageContext.Provider>;
  }
  const root = renderSync(<InputProvider><Fixture project={initial}/></InputProvider>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {
    async send(key: string) { stdin.write(key); await wait(); },
    async project(project: string) { root.rerender(<InputProvider><Fixture project={project}/></InputProvider>); await wait(); },
    async resize(width: number, height = 24) { stdout.columns = width; stdout.rows = height; stdout.emit("resize"); await wait(); },
    text() { const screen = (instances.get(stdout) as any).frontFrame.screen;
      return Array.from({length: screen.height}, (_, y) => Array.from({length: screen.width}, (_, x) => cellAt(screen, x, y)?.char || "").join("")).join("\n"); },
    footer: () => footer, dismissed: () => dismissed,
    viewport() { const find = (node: any): any => node.scrollViewportHeight !== undefined ? node : (node.childNodes ?? []).map(find).find(Boolean); return find((instances.get(stdout) as any).rootNode); },
    close() { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); },
  };
}

test("platform viewing is offline and drafting never sends until the explicit draft command", async () => {
  const calls: {op: string; args: any}[] = [];
  const value = candidate("one");
  const row: any = value.items[0];
  Object.assign(row, {allowed_actions: ["collaborate", "collaborate_send"], collaboration: {snapshots: [{id: "snapshot-fixed", binding: "current",
    target: {kind: "github", instance: "https://github.com", repository: "test/fixture", account: "tester"}, resource: {kind: "issue", number: 3}, page: 1,
    document: {id: "3", title: "Saved platform issue", body: "Already saved"}}], drafts: []}});
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return value;
    if (op === "publish.collaborate") row.collaboration.drafts = [{id: "draft-fixed", binding: "current", state: "draft",
      target: row.collaboration.snapshots[0].target, resource: {kind: "issue", number: 3}, content: args.draft}];
    return {request_id: args.request_id, operation_id: "save", effect_state: "committed", item: row};
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); };
  try {
    await wait(); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("Saved platform issue");
    expect(calls.every(c => c.op === "publish.items")).toBe(true);
    await command("comment"); await io.send("Review this fixed candidate"); await io.send("\r");
    const saved = calls.find(c => c.op === "publish.collaborate")!;
    expect(saved.args).toMatchObject({action: "draft", item_id: "fixed-item", expected_revision: 1, snapshot_id: "snapshot-fixed",
      draft: {kind: "comment", body: "Review this fixed candidate"}});
    expect(calls.some(c => c.op === "publish.collaborate.send")).toBe(false);
    await command("send draft-fixed");
    expect(calls.find(c => c.op === "publish.collaborate.send")!.args.draft_id).toBe("draft-fixed");
  } finally { io.close(); }
});

test("linked task opens the exact existing conversation, parent and details then restores Publish scroll", async () => {
  const state = createPublishStateStore().scope("one");
  const calls: string[] = [];
  const row = candidate("one");
  Object.assign(row.items[0], {task_links: [{id: "link", process_id: "child", linked_task_id: "child-task", task_id: "actor-task",
    parent_process_id: "main", parent_task_id: "main-task"}], evidence: Array.from({length: 50}, (_, i) => ({id: String(i), kind: "check", outcome: "failed", summary: "long record " + i}))});
  const client: any = {async callTool(op: string, args: any) {
    calls.push(op);
    if (op === "publish.items") return row;
    if (op === "publish.task") return {project: args.project,
      process: {process_id: args.process_id, active_task_id: args.task_id, actor_kind: args.process_id === "main" ? "supervisor" : "worker", status: "completed", display_name: "Linked process"},
      messages: [{role: "assistant", content: args.process_id === "main" ? "EXACT-PARENT-CONVERSATION" : "EXACT-CHILD-CONVERSATION", timestamp: "now", process_id: args.process_id, turn_id: args.task_id},
        {role: "assistant", content: "UNRELATED-PRIVATE-CONVERSATION", timestamp: "now", turn_id: "other"}]};
    throw new Error(op);
  }};
  const io = mount(client, "one", state);
  try {
    await wait(); await io.send("\r"); await io.send("\r"); for (let i = 0; i < 6; i++) await io.send("\x1b[B");
    const top = io.viewport().scrollTop; expect(top).toBeGreaterThan(0);
    await io.send("/"); await io.send("task 1"); await io.send("\r");
    expect(io.text()).toContain("EXACT-CHILD-CONVERSATION"); expect(io.text()).not.toContain("actor-task");
    await io.send("d"); expect(io.text()).toContain("进程 ID"); await io.send("\x1b");
    await io.send("p"); expect(io.text()).toContain("EXACT-PARENT-CONVERSATION"); expect(io.text()).not.toContain("UNRELATED-PRIVATE-CONVERSATION");
    await io.send("\x1b"); expect(io.viewport().scrollTop).toBe(top);
    expect(calls).toEqual(["publish.items", "publish.task", "publish.task"]);
  } finally { io.close(); }
});

test("a replaced linked task never displays another task's conversation", async () => {
  const value = candidate("one"); Object.assign(value.items[0], {task_links: [{id: "link", process_id: "child", linked_task_id: "old-task", task_id: "actor"}]});
  const client: any = {async callTool(op: string) { return op === "publish.items" ? value : {project: "one", process: {process_id: "child", active_task_id: "new-task"},
    messages: [{role: "assistant", content: "WRONG-TASK-SECRET", timestamp: "now", turn_id: "new-task"}]}; }};
  const io = mount(client);
  try { await wait(); await io.send("/"); await io.send("task"); await io.send("\r");
    expect(io.text()).toContain("身份发生变化"); expect(io.text()).not.toContain("WRONG-TASK-SECRET");
    await io.send("\x1b"); expect(io.text()).toContain("本地已保存记录");
  } finally { io.close(); }
});

test("unfinished forms survive leaving and remounting, and changed revisions require an explicit reset", async () => {
  const state = createPublishStateStore().scope("one"); let revision = 1;
  const writes: any[] = [];
  const client: any = {async callTool(op: string, args: any) {
    if (op === "publish.items") return candidate("one", "prepared", revision);
    writes.push(args); return {request_id: args.request_id, effect_state: "committed", operation_id: "saved"};
  }};
  let io = mount(client, "one", state);
  try {
    await wait(); await io.send("/"); await io.send("plan"); await io.send("\r");
    await io.send("\x15"); await io.send("publish/中文草稿🚀"); await io.send("\r");
    await io.send("\x15"); await io.send("未保存负责人"); await io.send("\x1b");
    await io.send("/"); await io.send("plan"); await io.send("\r"); expect(io.text()).toContain("未保存负责人");
    io.close(); io = mount(client, "one", state); await wait();
    expect(io.text()).toContain("publish/中文草稿🚀"); expect(io.footer().cmdInput.value).toBe("未保存负责人");
    io.close(); revision = 7; io = mount(client, "one", state); await wait();
    expect(io.text()).toContain("旧草稿不能直接保存"); await io.send("\r"); await io.send("\r"); expect(writes).toHaveLength(0);
    await io.send("\x12"); expect(io.text()).not.toContain("旧草稿不能直接保存");
    await io.send("\r"); await io.send("\r"); await io.send("\r");
    expect(writes[0].expected_revision).toBe(7); expect(state.get("form/draft/plan/fixed-item")).toBeUndefined();
  } finally { io.close(); }
});

test("saved item, node and scroll restore without running a business operation", async () => {
  const state = createPublishStateStore().scope("one"), calls: string[] = [];
  const client: any = {async callTool(op: string) { calls.push(op); const value = snapshot("one");
    Object.assign(value.items[1], {...candidate("one").items[0], id: "one-1", title: "候选 🚀 1", evidence: Array.from({length: 60}, (_, i) => ({id: String(i), kind: "check", outcome: "passed", summary: "saved long record " + i}))});
    return value;
  }};
  let io = mount(client, "one", state);
  try {
    await wait(); await io.send("\r"); await io.send("\x1b[B"); await io.send("\x1b[C"); await io.send("\r"); for (let i = 0; i < 6; i++) await io.send("\x1b[B");
    const before = state.get<any>("view"); expect(before.selectedId).toBe("one-1"); expect(before.nodeId).toBe("integrate");
    const top = io.viewport().scrollTop; expect(top).toBeGreaterThan(0);
    io.close(); io = mount(client, "one", state); await wait();
    expect(state.get<any>("view").selectedId).toBe("one-1"); expect(io.viewport().scrollTop).toBe(top);
    expect(state.get<any>("view").nodeId).toBe("integrate"); expect(calls).toEqual(["publish.items", "publish.items"]);
  } finally { io.close(); }
});

test("history retains merge lanes across pages and restores the selected parent diff after remount", async () => {
  const state = createPublishStateStore().scope("one"), calls: {op: string; args: any}[] = [];
  const oid = (digit: string) => digit.repeat(40);
  const commit = (digit: string, parents: string[]) => ({oid: oid(digit), parents: parents.map(oid), subject: "Commit " + digit, author: {name: "Author", email: "a@b", timestamp: 0}});
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return candidate("one");
    if (op === "publish.history") return {snapshot_id: "fixed", head_oid: oid("a"), relation: "git_parent",
      commits: args.cursor ? [commit("c", ["d"]), commit("d", [])] : [commit("a", ["b", "c"]), commit("b", ["d"])], next_cursor: args.cursor ? null : "second"};
    if (op === "publish.diff") return {text: Array.from({length: 80}, (_, i) => "diff line " + i).join("\n"), base_oid: args.base_oid, head_oid: args.head_oid};
    throw new Error(op);
  }};
  let io = mount(client, "one", state);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); };
  try {
    await wait(); await command("history"); expect(io.text()).toContain("02");
    await command("more"); expect(io.text()).toContain("02");
    expect(state.get<any>("history/fixed-item/candidate").frontier).toEqual([oid("d"), oid("c")]);
    for (const [width, height] of [[120, 32], [80, 24], [40, 20]]) { await io.resize(width!, height!); expect(io.text()).toContain("Git 分支 / 合并图"); }
    await io.send("\x1b"); await command("parent 2");
    expect(calls.filter(row => row.op === "publish.diff").at(-1)!.args.base_oid).toBe(oid("c"));
    for (let i = 0; i < 4; i++) await io.send("\x1b[B");
    const top = io.viewport().scrollTop; expect(top).toBeGreaterThan(0);
    io.close(); io = mount(client, "one", state); await wait(); await wait();
    expect(io.text()).toContain("diff line"); expect(io.viewport().scrollTop).toBe(top);
    expect(calls.filter(row => row.op === "publish.diff").at(-1)!.args.base_oid).toBe(oid("c"));
    expect(calls.every(row => ["publish.items", "publish.history", "publish.diff"].includes(row.op))).toBe(true);
  } finally { io.close(); }
});

test("settings input survives leaving and restarting without silently overwriting changed configuration", async () => {
  const state = createPublishStateStore().scope("one"); let prefix = "OLD"; const writes: any[] = [];
  const client: any = {async callTool(op: string, args: any) {
    if (op === "publish.items") return candidate("one");
    if (op === "publish.get") return {commit_format: {prefix}, privacy: {}, remote_url: "", stages: {}};
    if (op === "history.list") return [];
    writes.push(args); return {};
  }};
  let io = mount(client, "one", state);
  try {
    await wait(); await io.send("/"); await io.send("settings"); await io.send("\r"); await io.send("\r");
    await io.send("\x1b[H"); await io.send("中文🚀");
    const value = io.footer().cmdInput.value;
    io.close(); io = mount(client, "one", state); await wait(); expect(io.footer().cmdInput.value).toBe(value);
    await io.send("\x1b"); await io.send("\r"); expect(io.footer().cmdInput.value).toBe(value);
    io.close(); prefix = "CHANGED"; io = mount(client, "one", state); await wait();
    expect(io.text()).toContain("配置已变化"); await io.send("\r"); expect(writes).toHaveLength(0);
  } finally { io.close(); }
});

test("account credentials remain masked in the content and command bar", async () => {
  const calls: {op: string; args: any}[] = [];
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return candidate(args.project);
    return {request_id: args.request_id, operation_id: "account", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("account"); await io.send("\r");
    await io.send("\r"); await io.send("\r");
    await io.send("test/fixture"); await io.send("\r"); await io.send("tester"); await io.send("\r");
    await io.send("test-only-secret-value");
    expect(io.text()).not.toContain("test-only-secret-value");
    expect(io.footer().cmdInput.value).toBe("•".repeat(22));
    expect(calls.some(c => c.op === "publish.account.save")).toBe(false);
    await io.send("\r");
    expect(calls.find(c => c.op === "publish.account.save")!.args.token).toBe("test-only-secret-value");
  } finally { io.close(); }
});

test("shared facts and real Git parents are opened only by explicit readonly commands", async () => {
  const calls: {op: string; args: any}[] = [];
  const value = candidate("one");
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return value;
    if (op === "publish.shared.items") return {schema_version: 1, item_id: "fixed-item", observation_id: "saved-shared", snapshot_id: "shared-hash",
      remote: "team", observed_oid: "d".repeat(40), total: 1, read_only: true, next_cursor: null,
      items: [{id: "colleague-item", title: "Shared contribution", owner: "colleague", rework: "Follow up", reviews: [], result_oids: []}]};
    if (op === "publish.history") return {snapshot_id: "git-snapshot", head_oid: "a".repeat(40), relation: "git_parent", next_cursor: null,
      commits: [{oid: "a".repeat(40), parents: ["b".repeat(40)], subject: "Actual parent", author: {name: "Fixture", email: "fixture@example.invalid", timestamp: 1}}]};
    if (op === "publish.diff") return {base_oid: "b".repeat(40), head_oid: "a".repeat(40), text: "+fixed tree content", truncated: false};
    throw new Error("unexpected mutation");
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); };
  try {
    await wait(); expect(calls.map(c => c.op)).toEqual(["publish.items"]);
    await command("shared"); expect(io.text()).toContain("Shared contribution");
    await io.send("\x1b"); await command("history");
    expect(io.text()).toContain("Git 分支 / 合并图"); expect(io.text()).toContain("父提交: bbbbbbbbbbbb");
    expect(calls.some(c => c.op === "publish.diff")).toBe(false);
    await io.send("\r"); expect(io.text()).toContain("+fixed tree content");
    expect(calls.find(c => c.op === "publish.diff")!.args).toMatchObject({item_id: "fixed-item", snapshot_id: "git-snapshot", head_oid: "a".repeat(40), base_oid: "b".repeat(40)});
    await io.send("\x1b"); expect(io.text()).toContain("Actual parent");
    await io.send("\x1b"); expect(io.text()).toContain("待接手贡献");
  } finally { io.close(); }
});

test("native build remains cancellable after submitting its recipe", async () => {
  const calls: {op: string; args: any}[] = [];
  const value = candidate("one", "integrated"); const row: any = value.items[0]; row.allowed_actions = ["package"];
  let finish: ((value: any) => void) | undefined, buildArgs: any;
  const client: any = {callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return Promise.resolve(value);
    if (op === "publish.item") return Promise.resolve({item: {...row, revision: 4, allowed_actions: ["pause", "distribution_reconcile"]}});
    if (op === "publish.package") { buildArgs = args; return new Promise(resolve => { finish = resolve; }); }
    return Promise.resolve({request_id: args.request_id, operation_id: "pause", effect_state: "committed", item: row});
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); };
  try {
    await wait(); await command("package"); await io.send("v1.2.3"); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("可用 /pause 请求暂停"); await command("pause");
    expect(calls.find(c => c.op === "publish.pause")!.args.expected_revision).toBe(4);
    finish!({request_id: buildArgs.request_id, operation_id: "build", effect_state: "committed", item: row}); await wait();
    expect(calls.filter(c => c.op === "publish.package")).toHaveLength(1);
  } finally { io.close(); }
});

test("branch deletion is a reviewed plan before a separate application", async () => {
  const calls: {op: string; args: any}[] = [];
  const value = candidate("one", "adopted");
  const row: any = value.items[0]; row.allowed_actions = ["branch", "branch_apply"];
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return value;
    return {request_id: args.request_id, operation_id: "branch", effect_state: "committed", item: row};
  }};
  const io = mount(client);
  const command = async (name: string) => { await io.send("/"); await io.send(name); await io.send("\r"); };
  try {
    await wait(); await command("branch"); await io.send("\x15"); await io.send("prepare_delete"); await io.send("\r");
    await io.send("\r"); await io.send("\r"); await io.send("Merged and retained in archive"); await io.send("\r"); await io.send("\r");
    expect(calls.find(c => c.op === "publish.branch")!.args).toMatchObject({action: "prepare_delete", branch: "refs/heads/publish/work", owner: "maintainer"});
    expect(calls.some(c => c.op === "publish.branch.apply")).toBe(false);
    await command("branch-apply");
    expect(calls.find(c => c.op === "publish.branch.apply")!.args).toMatchObject({item_id: "fixed-item", expected_revision: 1});
  } finally { io.close(); }
});

test("build recipe fields appear after choosing a script and bind declared outputs", async () => {
  const calls: {op: string; args: any}[] = [];
  const value = candidate("one", "integrated"); const row: any = value.items[0]; row.allowed_actions = ["package"];
  const client: any = {async callTool(op: string, args: any) {
    calls.push({op, args});
    if (op === "publish.items") return value;
    return {request_id: args.request_id, operation_id: "build", effect_state: "committed", item: row};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("package"); await io.send("\r");
    await io.send("v1.2.3"); await io.send("\r"); await io.send("build.py"); await io.send("\r");
    expect(io.text()).toContain("构建输出文件");
    expect(calls.some(c => c.op === "publish.package")).toBe(false);
    await io.send("dist/app.zip;dist/checksum.txt"); await io.send("\r"); await io.send("\r");
    expect(calls.find(c => c.op === "publish.package")!.args).toMatchObject({version: "v1.2.3",
      recipe: {entry: "build.py", outputs: ["dist/app.zip", "dist/checksum.txt"], timeout: 120}});
  } finally { io.close(); }
});

test("Publish navigates actual query observations, returns to selection and never mutates while browsing", async () => {
  const calls: string[] = [];
  const client: any = {async callTool(operation: string, args: any) { calls.push(operation); return snapshot(args.project); }};
  const io = mount(client);
  try {
    await wait(); expect(io.text()).toContain("待核对"); expect(io.footer().kind).toBe("command");
    await io.send("\r"); await io.send("\x1b[B"); await io.send("\x1b[C"); await io.send("\r");
    expect(io.text()).toContain("候选 🚀 1"); expect(io.text()).toContain("实际结果提交");
    await io.send("\x1b"); expect(io.text()).toContain("[合入]"); expect(io.text()).toContain("2/3");
    await io.send("/"); await io.send("refresh"); await io.send("\r");
    expect(io.text()).toContain("2/3"); expect(io.text()).toContain("[合入]");
    for (const width of [80, 40, 120]) { await io.resize(width); expect(io.text()).toContain("待核对"); }
    expect(calls).toEqual(["publish.items", "publish.items"]);
    await io.send("\x1b"); await io.send("\x1b"); expect(io.dismissed()).toBe(1);
  } finally { io.close(); }
});

test("late responses from the previous project cannot overwrite Publish", async () => {
  let finish: ((value: any) => void) | undefined;
  const client: any = {callTool(_operation: string, args: any) {
    if (args.project === "one") return new Promise(resolve => { finish = resolve; });
    return Promise.resolve(snapshot(args.project, "新项目"));
  }};
  const io = mount(client);
  try {
    await wait(); await io.project("two"); await wait();
    finish!(snapshot("one", "旧项目")); await wait();
    expect(io.text()).toContain("新项目"); expect(io.text()).not.toContain("旧项目");
  } finally { io.close(); }
});

test("out of order refreshes retain the newest observation and errors stay visible", async () => {
  let finish: ((value: any) => void) | undefined; let count = 0;
  const client: any = {callTool(_operation: string, args: any) {
    count++;
    if (count === 2) return new Promise(resolve => { finish = resolve; });
    if (count === 4) return Promise.reject(new Error("PUBLISH_STATE_UNAVAILABLE"));
    return Promise.resolve(snapshot(args.project, count === 3 ? "最新内容" : "初始内容"));
  }};
  const io = mount(client);
  const refresh = async () => { await io.send("/"); await io.send("refresh"); await io.send("\r"); };
  try {
    await wait(); await refresh(); await refresh(); finish!(snapshot("one", "迟到内容")); await wait();
    expect(io.text()).toContain("最新内容"); expect(io.text()).not.toContain("迟到内容");
    await refresh(); expect(io.text()).toContain("已有记录可能过期"); expect(io.text()).toContain("最新内容");
  } finally { io.close(); }
});

test("settings retain the Alpha3 configuration entry and return to Publish", async () => {
  const calls: string[] = [];
  const client: any = {async callTool(operation: string, args: any) {
    calls.push(operation);
    if (operation === "publish.items") return snapshot(args.project);
    if (operation === "history.list") return [];
    if (operation === "publish.get") return {commit_format: {}, privacy: {}, remote_url: "", stages: {}};
    throw Error("Unexpected mutation");
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("\x1b[C"); await io.send("\r");
    expect(io.text()).toContain("提交格式"); expect(io.text()).toContain("隐私"); expect(io.text()).toContain("远端");
    await io.send("\x1b"); await io.send("\x1b[D"); await io.send("\r");
    expect(io.text()).toContain("待核对"); expect(calls.some(name => name.endsWith(".set"))).toBe(false);
  } finally { io.close(); }
});

test("long lists remain navigable in a narrow terminal without executing future nodes", async () => {
  const calls: string[] = [];
  const client: any = {async callTool(operation: string, args: any) { calls.push(operation); return snapshot(args.project, "候选", 50); }};
  const io = mount(client);
  try {
    await wait(); await io.resize(40, 20); await io.send("\r");
    for (let index = 0; index < 12; index++) await io.send("\x1b[B");
    expect(io.text()).toContain("TEST-12"); expect(io.text()).toContain("13/50");
    await io.send("\x1b[C"); await io.send("\x1b[C"); await io.send("\r");
    expect(io.text()).toContain("远端结果尚待核对"); expect(calls).toEqual(["publish.items"]);
  } finally { io.close(); }
});

test("legacy migration explicitly binds the selected observation and keeps the old flags out of the command", async () => {
  let saved: any;
  const client: any = {async callTool(operation: string, args: any) {
    if (operation === "publish.items") {
      const value = snapshot(args.project);
      Object.assign(value.items[0], {allowed_actions: ["migrate"]});
      return value;
    }
    saved = args;
    expect(operation).toBe("publish.migrate");
    return {request_id: args.request_id, operation_id: "migration", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("migrate"); await io.send("\r");
    expect(io.text()).toContain("将备份旧记录");
    await io.send("\r"); await io.send("\r"); await io.send("formal"); await io.send("\r");
    await io.send("\r"); await io.send("maintainer"); await io.send("\r");
    await io.send("test_release.py"); await io.send("\r"); await io.send("\r");
    expect(saved.legacy_id).toBe(snapshot("one").items[0].id);
    expect(saved.source_ref).toBe("HEAD"); expect(saved.target_branch).toBe("formal");
    expect(saved).not.toHaveProperty("item_id"); expect(saved).not.toHaveProperty("synced");
  } finally { io.close(); }
});

test("review drafts send scoped decisions with notes and owner", async () => {
  let saved: any;
  const client: any = {async callTool(operation: string, args: any) {
    const value = candidate(args.project);
    Object.assign(value.items[0], {allowed_actions: ["decide"]});
    if (operation === "publish.items") return value;
    saved = args; expect(operation).toBe("publish.decide");
    return {request_id: args.request_id, operation_id: "review", effect_state: "committed"};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("decide"); await io.send("\r");
    expect(io.text()).toContain("审阅固定候选");
    await io.send("\x15"); await io.send("rework"); await io.send("\r"); await io.send("\r");
    await io.send("修复失败测试"); await io.send("\r");
    expect(saved.decision).toBe("rework"); expect(saved.note).toBe("修复失败测试");
    expect(saved.item_id).toBe("fixed-item"); expect(saved.expected_revision).toBe(1);
  } finally { io.close(); }
});

test("run requires explicit steps and polls the background task without repeating mutations", async () => {
  const writes: any[] = []; let reads = 0; let started = false;
  const client: any = {async callTool(operation: string, args: any) {
    const value = candidate(args.project);
    Object.assign(value.items[0], {allowed_actions: ["run"]});
    if (started) Object.assign(value.items[0], {run: {id: "job", state: "running", steps: ["verify", "execute"],
      next: 0, stop_after: "execute", actor: "local_operator", task_id: "", observations: []},
      reason: "PUBLISH_RUN_RUNNING", allowed_actions: ["pause", "reconcile"]});
    if (operation === "publish.items") { reads++; return value; }
    writes.push({operation, args}); started = true;
    return {request_id: args.request_id, operation_id: "job", effect_state: "committed", item: value.items[0]};
  }};
  const io = mount(client);
  try {
    await wait(); await io.send("/"); await io.send("run"); await io.send("\r");
    expect(writes).toHaveLength(0); expect(io.text()).toContain("/run verify");
    await io.send("/"); await io.send("run verify execute"); await io.send("\r");
    expect(writes).toHaveLength(1);
    expect(writes[0].operation).toBe("publish.run");
    expect(writes[0].args.steps).toEqual(["verify", "execute"]);
    const before = reads;
    await new Promise(resolve => setTimeout(resolve, 1100)); await wait();
    expect(reads).toBeGreaterThan(before); expect(writes).toHaveLength(1);
    expect(io.text()).toContain("正在推进已授权步骤");
    await io.send("/"); await io.send("pause"); await io.send("\r");
    expect(writes[1].operation).toBe("publish.pause");
  } finally { io.close(); }
});

test("opening long matrix evidence starts at the plan instead of following the log tail", async () => {
  const client: any = {async callTool(_operation: string, args: any) {
    const value = candidate(args.project);
    Object.assign(value.items[0], {plan: {destination: "formal", method: "squash", tree_oid: "c".repeat(40)},
      evidence: [{id: "test", binding: "current", kind: "project_test", outcome: "passed", summary: "output\n".repeat(120)}]});
    return value;
  }};
  const io = mount(client);
  try {
    await wait(); await io.resize(80, 24); await io.send("\r"); await io.send("\r");
    expect(io.text()).toContain("最终内容树"); expect(io.text()).toContain("源提交");
    for (const [width, height] of [[40, 20], [80, 24]]) {
      await io.resize(width, height);
      const rows = io.text().split("\n");
      expect(rows.some(row => row.includes("本地已保存记录"))).toBe(true);
      expect(rows.find(row => row.includes("Esc 返回事项"))?.trim()).toBe("↑/↓ 滚动 · Esc 返回事项");
    }
    for (let i = 0; i < 8; i++) await io.send("\x1b[B");
    await io.send("\x1b"); await io.send("\r");
    expect(io.text()).toContain("最终内容树");
  } finally { io.close(); }
});
