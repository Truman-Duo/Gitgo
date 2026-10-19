import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Text} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {useTextInput} from "../hooks/useTextInput.js";
import {LessonsPanel} from "./LessonsPanel.js";
import {BtwPanel} from "./BtwPanel.js";
import {useChat} from "../hooks/useChat.js";
import type {BackendClient} from "../backend/client.js";

const proposal = {proposal_id: "proposal", proposal_version: "shown-version", created_at: "2026-10-13",
  candidates: [0, 1].map(index => ({trigger: "adapter-" + index, rule: "A concrete compatibility rule of sufficient length",
    qualification: {knowledge_type: "compatibility", conditions: "Build 3 only", validation: "One regression passed",
      novelty: "Private undocumented behavior", limitations: "No other release tested"}}))};
function terminal() {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 110; stdout.rows = 35;
  let output = ""; stdout.on("data", (chunk: Buffer) => {output += chunk.toString("utf8");});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  return {stdin, stdout, stderr, output: () => output};
}
const settle = () => new Promise(resolve => setTimeout(resolve, 80));

test("real lesson component edits only a draft, excludes a candidate and submits the displayed version", async () => {
  const t = terminal(), mutations: any[] = [], btw: any[] = [];
  let draft: ReturnType<typeof useTextInput>;
  let waiting = true;
  const client: BackendClient = {ready: true, close() {}, async callTool(operation, args: any = {}) {
    if (operation === "lesson.list") return {pending: [], instances: [], abstract: []};
    if (operation === "lesson.harvest.list") return {proposals: waiting ? [proposal] : []};
    if (operation === "runtime.btw") {btw.push(args); return {answer: "Only build 3 has evidence.", sidecar_id: args.sidecar_id};}
    if (operation === "lesson.harvest.resolve") {mutations.push(args); waiting = false; return {status: "accepted", count: 1};}
    throw new Error(operation);
  }};
  function Fixture() {
    draft = useTextInput("");
    return <InputProvider><LessonsPanel client={client} project="p" cols={110} cmdInput={draft} onFooter={() => {}} onDismiss={() => {}}/></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {...t, patchConsole: false, exitOnCtrlC: false});
  const send = async (key: string) => {t.stdin.write(key); await settle();};
  try {
    await settle(); await send("p"); await send("e");
    draft!.setValue("revised adapter trigger"); await settle(); await send("\t");
    draft!.setValue("Keep the private gateway token under metadata for build 3 only."); await settle(); await send("\r");
    expect(mutations).toHaveLength(0);
    await send("b"); await settle();
    expect(btw[0].lesson_proposal).toMatchObject({proposal_id: "proposal", proposal_version: "shown-version", candidate_index: 0,
      draft: {trigger: "revised adapter trigger", rule: "Keep the private gateway token under metadata for build 3 only."}});
    expect(t.output()).toContain("Return to review");
    expect(t.output()).not.toContain("Interrupt & apply");
    await send("\u001b"); await settle();
    expect(mutations).toHaveLength(0);
    await send("\u001b[B"); await send(" "); await send("s");
    expect(mutations).toHaveLength(1);
    expect(mutations[0].expected_version).toBe("shown-version");
    expect(mutations[0].candidate_reviews[0].trigger).toBe("revised adapter trigger");
    expect(mutations[0].candidate_reviews[0].rule).toContain("metadata");
    expect(mutations[0].candidate_reviews[1]).toEqual({index: 1, action: "discard"});
  } finally {root.unmount(); root.cleanup(); t.stdin.destroy(); t.stdout.destroy(); t.stderr.destroy();}
});

test("proposal hydration cannot restore a closed notice and repeated live events do not duplicate it", async () => {
  const t = terminal();
  let callback: any, resolve: any, hook: ReturnType<typeof useChat>;
  const client: BackendClient = {ready: true, close() {},
    subscribeEvents(listener) {callback = listener; return () => {};},
    callTool() {return new Promise(done => {resolve = done;});}};
  function Fixture() {hook = useChat(client, "p"); return <Text>{hook.messages.map(row => row.content).join("\n")}</Text>;}
  const root = renderSync(<Fixture/>, {...t, patchConsole: false, exitOnCtrlC: false});
  try {
    await settle();
    callback({event: "lesson_harvest_proposed", project: "p", proposal});
    callback({event: "lesson_harvest_proposed", project: "p", proposal});
    await settle();
    expect(hook!.messages.filter(row => row.kind === "knowledge_notice")).toHaveLength(1);
    callback({event: "lesson_harvest_resolved", project: "p", proposal_id: proposal.proposal_id, status: "dismissed"});
    resolve({proposals: [proposal]}); await settle();
    expect(hook!.messages.filter(row => row.kind === "knowledge_notice")).toHaveLength(0);
    callback({event: "lesson_harvest_proposed", project: "p", proposal}); await settle();
    expect(hook!.messages.filter(row => row.kind === "knowledge_notice")).toHaveLength(0);
  } finally {root.unmount(); root.cleanup(); t.stdin.destroy(); t.stdout.destroy(); t.stderr.destroy();}
});


test("stale confirmation retains the old draft and never silently confirms a refreshed proposal", async () => {
  const t = terminal(), attempts: any[] = [];
  const client: BackendClient = {ready: true, close() {}, async callTool(operation, args: any = {}) {
    if (operation === "lesson.list") return {pending: [], instances: [], abstract: []};
    if (operation === "lesson.harvest.list") return {proposals: [proposal]};
    if (operation === "lesson.harvest.resolve") {attempts.push(args); throw new Error("HARVEST_PROPOSAL_CHANGED");}
    throw new Error(operation);
  }};
  function Fixture() {
    const draft = useTextInput("");
    return <InputProvider><LessonsPanel client={client} project="p" cols={110} cmdInput={draft} onFooter={() => {}} onDismiss={() => {}}/></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {...t, patchConsole: false, exitOnCtrlC: false});
  try {
    await settle(); t.stdin.write("p"); await settle();
    t.stdin.write("s"); await settle();
    expect(attempts).toHaveLength(1); expect(attempts[0].expected_version).toBe("shown-version");
    expect(t.output()).toContain("HARVEST_PROPOSAL_CHANGED");
    expect(t.output()).toContain("Review knowledge proposal");
    expect(t.output()).not.toContain("Saved 1 reviewed");
  } finally {root.unmount(); root.cleanup(); t.stdin.destroy(); t.stdout.destroy(); t.stderr.destroy();}
});

test("closing an in-flight lesson discussion cancels its known sidecar instead of saving or applying", async () => {
  const t = terminal(), calls: any[] = []; let finish: any;
  const client: BackendClient = {ready: true, close() {}, async callTool(operation, args: any = {}) {
    calls.push({operation, args});
    if (operation === "lesson.list") return {pending: [], instances: [], abstract: []};
    if (operation === "lesson.harvest.list") return {proposals: [proposal]};
    if (operation === "runtime.btw") return new Promise(resolve => {finish = resolve;});
    if (operation === "runtime.btw.cancel") {finish({answer: "Cancelled", sidecar_id: args.sidecar_id}); return {cancelled: true};}
    throw new Error(operation);
  }};
  function Fixture() {
    const draft = useTextInput("");
    return <InputProvider><LessonsPanel client={client} project="p" cols={110} cmdInput={draft} onFooter={() => {}} onDismiss={() => {}}/></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {...t, patchConsole: false, exitOnCtrlC: false});
  try {
    await settle(); t.stdin.write("p"); await settle(); t.stdin.write("b"); await settle();
    const request = calls.find(call => call.operation === "runtime.btw");
    expect(request.args.sidecar_id).toBeTruthy();
    t.stdin.write("\u001b"); await settle(); await settle();
    expect(calls.find(call => call.operation === "runtime.btw.cancel").args.sidecar_id).toBe(request.args.sidecar_id);
    expect(calls.some(call => ["lesson.harvest.resolve", "runtime.feedback", "runtime.chat"].includes(call.operation))).toBe(false);
  } finally {root.unmount(); root.cleanup(); t.stdin.destroy(); t.stdout.destroy(); t.stderr.destroy();}
});

test("review exposes the selected native passages without accepting the proposal", async () => {
  const t = terminal(); const calls: string[] = [];
  const selected = {...proposal, candidates: [{...proposal.candidates[0], evidence: {workspace_excerpts: [{
    path: "adapter.py", start_line: 8, end_line: 12, text: "verified captured metadata passage",
  }]}}]};
  const client: BackendClient = {ready: true, close() {}, async callTool(operation) {
    calls.push(operation);
    if (operation === "lesson.list") return {pending: [], instances: [], abstract: []};
    if (operation === "lesson.harvest.list") return {proposals: [selected]};
    if (operation === "lesson.harvest.context") return {sources: [], evidence_binding: {
      selected_excerpts: selected.candidates[0].evidence.workspace_excerpts,
    }};
    throw new Error(operation);
  }};
  function Fixture() {
    const draft = useTextInput("");
    return <InputProvider><LessonsPanel client={client} project="p" cols={110} cmdInput={draft} onFooter={() => {}} onDismiss={() => {}}/></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {...t, patchConsole: false, exitOnCtrlC: false});
  try {
    await settle(); t.stdin.write("p"); await settle();
    expect(t.output()).toContain("Evidence covers selected passages");
    t.stdin.write("f"); await settle();
    expect(t.output()).toContain("verified captured metadata passage");
    expect(t.output()).toContain("Snapshot from the change");
    expect(calls).not.toContain("lesson.harvest.resolve");
  } finally {root.unmount(); root.cleanup(); t.stdin.destroy(); t.stdout.destroy(); t.stderr.destroy();}
});


test("BTW yields a checker request to the canonical question queue without granting or applying it", async () => {
  const t = terminal(), calls: string[] = [];
  let dismissed = 0, applied = 0;
  const client: BackendClient = {ready: true, close() {}, async callTool(operation, args: any = {}) {
    calls.push(operation);
    if (operation === "runtime.btw") return {sidecar_id: args.sidecar_id, status: "awaiting_user",
      decision_process_id: "sidecar", pending_decision: {decision_id: "real", kind: "permission"}};
    throw new Error(operation);
  }};
  const root = renderSync(<InputProvider><BtwPanel client={client} project="p" processId="" processes={{}}
    question="Enable the scoped check" answer="" sidecarId="test-card" lessonProposal={{proposal_id: "p", proposal_version: "v", candidate_index: 0}}
    onDismiss={() => {dismissed += 1;}} onApply={() => {applied += 1;}}/></InputProvider>, {...t, patchConsole: false, exitOnCtrlC: false});
  await settle(); await settle();
  expect(dismissed).toBe(1);
  expect(applied).toBe(0);
  expect(calls).toEqual(["runtime.btw"]);
  root.unmount();
});
