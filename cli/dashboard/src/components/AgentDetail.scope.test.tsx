import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import stripAnsi from "strip-ansi";
import {renderSync} from "@anthropic/ink";
import {InputProvider} from "../input/runtime.js";
import {AgentDetailScene} from "./AgentDetail.js";
import {getProcessStream} from "../daemon/processStreams.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 35));
function deferred() {let resolve!: (value: any) => void, reject!: (reason: Error) => void;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}
function mount() {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: false, columns: 100, rows: 24});
  const stderr: any = new PassThrough(); stderr.on("data", () => {}); let output = ""; stdout.on("data", (chunk: Buffer) => {output += chunk.toString();});
  const requests: Array<ReturnType<typeof deferred> & {args: any; onEvent: any}> = [];
  const client: any = {ready: true, close() {}, callTool(_operation: string, args: any, _timeout: number, onEvent: any) {
    const request = {...deferred(), args, onEvent}; requests.push(request); return request.promise;
  }};
  const send = {current: (_text: string) => {}}, scroll = {current: null};
  let scope = {project: "one", processId: "b-one", visible: true};
  const navigations: string[] = [], refreshes: string[] = [];
  function Fixture() {
    if (!scope.visible) return null;
    const process: any = {process_id: scope.processId, role: "worker", actor_kind: "worker", parent_id: "root",
      status: "completed", steps_used: 1, max_steps: 5, created_at: "1"};
    const requestedProject = scope.project;
    const loop: any = {loading: false, processes: {[scope.processId]: process}, toolEvents: [], agentConversations: {},
      refresh: async () => {refreshes.push(requestedProject);}};
    return <InputProvider><AgentDetailScene client={client} loopData={loop} activeProject={scope.project}
      activeAgentId={scope.processId} cols={100} rows={24} sendChatRef={send} scrollChatRef={scroll}
      decisionSelection={0} decisionComposing={false} decisionSubmitting={false} onDecisionChange={() => {}}
      onSendSettled={() => {}} onContinueAgent={processId => {
        navigations.push(processId); scope = {...scope, processId}; root.rerender(<Fixture/>);
      }}/></InputProvider>;
  }
  const root = renderSync(<Fixture/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {requests, navigations, refreshes, get scope() {return scope;}, send(text = "continue") {send.current(text);},
    get savedSend() {return send.current;}, text() {return stripAnsi(output);},
    async page(next: Partial<typeof scope>) {scope = {...scope, ...next}; root.rerender(<Fixture/>); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}};
}
const ack = (processId: string) => ({event: "runtime_ack", stage: "admitted", process_id: processId});

for (const change of ["project", "process", "scene"] as const) test(`late feedback ack/result cannot navigate after ${change} switch`, async () => {
  const view = mount();
  try {
    await wait(); view.send(); const request = view.requests[0];
    if (change === "project") await view.page({project: "two", processId: "b-two"});
    else if (change === "process") await view.page({processId: "b-other"});
    else await view.page({visible: false});
    request.onEvent(ack("b-one-successor"));
    request.onEvent({event: "text_delta", process_id: "b-one-successor", delta: "original project stream", visibility: "public"});
    request.resolve({process_id: "b-one-successor", outcome: {status: "completed"}}); await wait();
    expect(view.navigations).toEqual([]); expect(view.refreshes).toEqual([]);
    expect(view.scope.processId).not.toBe("b-one-successor");
    expect(getProcessStream("one", "b-one-successor")?.text).toContain("original project stream");
    expect(getProcessStream("two", "b-one-successor")).toBeNull();
  } finally {view.close();}
});

test("leaving then returning to the same identifiers does not revive the old request", async () => {
  const view = mount();
  try {
    await wait(); view.send(); const request = view.requests[0];
    await view.page({processId: "b-other"}); await view.page({processId: "b-one"});
    request.reject(new Error("STALE_ERROR_MUST_NOT_APPEAR")); await wait();
    expect(view.text()).not.toContain("STALE_ERROR_MUST_NOT_APPEAR"); expect(view.navigations).toEqual([]);
    view.send(); view.requests[1].reject(new Error("CURRENT_ERROR_MUST_APPEAR")); await wait();
    expect(view.text()).toContain("CURRENT_ERROR_MUST_APPEAR");
  } finally {view.close();}
});

test("a legitimate admitted successor keeps current completion and errors, without duplicate navigation", async () => {
  const view = mount();
  try {
    await wait(); view.send(); const request = view.requests[0];
    request.onEvent(ack("b-successor")); request.onEvent(ack("b-successor")); await wait();
    expect(view.navigations).toEqual(["b-successor"]); expect(view.scope.processId).toBe("b-successor");
    request.reject(new Error("SUCCESSOR_ERROR_VISIBLE")); await wait();
    expect(view.text()).toContain("SUCCESSOR_ERROR_VISIBLE");
    view.send(); view.requests[1].resolve({process_id: "b-successor", outcome: {status: "completed"}}); await wait();
    expect(view.text()).toContain("subprocess completed"); expect(view.refreshes).toEqual(["one"]);
    expect(view.navigations).toEqual(["b-successor"]);
  } finally {view.close();}
});

test("a newer send owns its feedback and stale send closures cannot target a previously selected process", async () => {
  const view = mount();
  try {
    await wait(); view.send("first"); const staleSend = view.savedSend;
    view.send("second"); view.requests[0].onEvent(ack("old-successor")); view.requests[0].reject(new Error("OLDER_REQUEST_ERROR"));
    view.requests[1].reject(new Error("NEWER_REQUEST_ERROR")); await wait();
    expect(view.navigations).toEqual([]); expect(view.text()).not.toContain("OLDER_REQUEST_ERROR"); expect(view.text()).toContain("NEWER_REQUEST_ERROR");
    await view.page({project: "two", processId: "b-two"}); staleSend("wrong target");
    expect(view.requests).toHaveLength(2);
  } finally {view.close();}
});


test("an ack arriving after the final result cannot replace its settled successor", async () => {
  const view = mount();
  try {
    await wait(); view.send(); const request = view.requests[0];
    request.resolve({process_id: "final-successor", outcome: {status: "completed"}}); await wait();
    request.onEvent(ack("late-successor")); await wait();
    expect(view.navigations).toEqual(["final-successor"]);
    expect(view.scope.processId).toBe("final-successor");
    expect(view.text()).not.toContain("continuation admitted");
  } finally {view.close();}
});
