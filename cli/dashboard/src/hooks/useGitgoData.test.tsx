import React from "react";
import {test, expect} from "bun:test";
import {PassThrough} from "node:stream";
import {Text, renderSync} from "@anthropic/ink";
import {useGitgoData} from "./useGitgoData.js";
import type {BackendClient} from "../backend/client.js";

const wait = () => new Promise(resolve => setTimeout(resolve, 35));
function deferred() {let resolve!: (value: any) => void, reject!: (reason: Error) => void;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}
const summary = (time: number | undefined, active: number, finished = 0) => ({
  ...(time === undefined ? {} : {observed_at: time}), daemon_online: true,
  active_process_count: active, finished_process_count: finished,
});
const overview = (value = summary(1, 0), identity = "id-one") => ({projects: [
  {name: "demo", project_id: identity, workspace: "/workspace", backup: "", commit_prefix: "", runtime_summary: value},
]});
function backend(response: () => any) {
  const listeners: Array<(event: any) => void> = []; let calls = 0;
  const client: BackendClient = {ready: true, close() {}, subscribeEvents(listener) {listeners.push(listener); return () => {};},
    async callTool() {calls++; return response();}};
  return {client, get calls() {return calls;}, emit(value: any, projectId?: string, projectName = "demo") {
    // Retain old callbacks intentionally: stale queued transport delivery must
    // remain harmless even after unsubscribe/reconnect.
    for (const listener of listeners) listener({event: "project_summary_updated", project: projectName, project_id: projectId, summary: value});
  }};
}
function mount(client: BackendClient) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: false, columns: 100, rows: 24}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  let current!: ReturnType<typeof useGitgoData>;
  function Fixture({client}: {client: BackendClient}) {current = useGitgoData(client); return <Text>projects</Text>;}
  const root = renderSync(<Fixture client={client}/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {get data() {return current;}, async reconnect(client: BackendClient) {root.rerender(<Fixture client={client}/>); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}};
}

test("newer overview repairs lost summary events and older/duplicate events cannot regress counts", async () => {
  let value = overview(); const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait(); transport.emit(summary(2, 1)); await wait(); expect(view.data.projects[0].activeProcessCount).toBe(1);
    value = overview(summary(3, 0, 1)); await view.data.refresh(); await wait();
    expect(view.data.projects[0].finishedProcessCount).toBe(1);
    transport.emit(summary(2, 1)); transport.emit(summary(3, 0, 1)); await wait();
    expect(view.data.projects[0].activeProcessCount).toBe(0); expect(view.data.projects[0].finishedProcessCount).toBe(1);
  } finally {view.close();}
});

test("a newer event received during an overview survives the older snapshot", async () => {
  const pending = deferred(), transport = backend(() => pending.promise), view = mount(transport.client);
  try {
    await wait(); transport.emit(summary(3, 0, 2)); pending.resolve(overview(summary(2, 1))); await wait();
    expect(view.data.projects[0].finishedProcessCount).toBe(2); expect(view.data.projects[0].activeProcessCount).toBe(0);
  } finally {view.close();}
});

test("legacy events without observation times only overlay their in-flight request", async () => {
  const pending = deferred(); let value: any = pending.promise;
  const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait(); transport.emit(summary(undefined, 1)); pending.resolve(overview(summary(undefined, 0))); await wait();
    expect(view.data.projects[0].activeProcessCount).toBe(1);
    value = overview(summary(undefined, 0, 1)); await view.data.refresh(); await wait();
    expect(view.data.projects[0].activeProcessCount).toBe(0); expect(view.data.projects[0].finishedProcessCount).toBe(1);
  } finally {view.close();}
});

test("project replacement drops the old identity's cached summary", async () => {
  let value = overview(); const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait(); transport.emit(summary(20, 3)); await wait();
    value = overview(summary(2, 0), "id-two"); await view.data.refresh(); await wait();
    expect(view.data.projects[0].project_id).toBe("id-two"); expect(view.data.projects[0].activeProcessCount).toBe(0);
  } finally {view.close();}
});

for (const late of ["success", "error"] as const) test(`reconnect fences old ${late}, subscriptions and in-flight request ownership`, async () => {
  const pending = deferred(), old = backend(() => pending.promise), fresh = backend(() => overview(summary(4, 0, 2))), view = mount(old.client);
  try {
    await wait(); await view.reconnect(fresh.client);
    expect(fresh.calls).toBe(1); expect(view.data.projects[0].finishedProcessCount).toBe(2);
    old.emit(summary(99, 99));
    if (late === "success") pending.resolve(overview(summary(99, 99)));
    else pending.reject(new Error("OLD_CONNECTION_FAILED"));
    await wait(); expect(view.data.projects[0].activeProcessCount).toBe(0); expect(view.data.error).toBeNull();
    await view.data.refresh(); expect(fresh.calls).toBe(2);
  } finally {view.close();}
});

test("current request errors remain visible and a later successful snapshot clears them", async () => {
  let fail = true; const transport = backend(() => {if (fail) throw new Error("CURRENT_HOST_FAILED"); return overview();}), view = mount(transport.client);
  try {
    await wait(); expect(view.data.error).toBe("CURRENT_HOST_FAILED"); expect(view.data.loading).toBe(false);
    fail = false; await view.data.refresh(); await wait(); expect(view.data.error).toBeNull(); expect(view.data.projects).toHaveLength(1);
  } finally {view.close();}
});


test("legacy overview workspace identity does not reject an event carrying a project UUID", async () => {
  const value = overview(); delete (value.projects[0] as any).project_id;
  const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait(); transport.emit(summary(2, 3), "id-one"); await wait();
    expect(view.data.projects[0].activeProcessCount).toBe(3);
    value.projects[0].project_id = "id-one"; await view.data.refresh(); await wait();
    transport.emit(summary(3, 99), "id-other"); await wait();
    expect(view.data.projects[0].activeProcessCount).not.toBe(99);
  } finally {view.close();}
});

test("non-Error rejection remains visible to the current view", async () => {
  const transport = backend(() => {throw "PLAIN_HOST_FAILURE";}), view = mount(transport.client);
  try {await wait(); expect(view.data.error).toBe("PLAIN_HOST_FAILURE");} finally {view.close();}
});

test("a removed project's late event cannot contaminate a new project with the same name", async () => {
  let value: any = overview();
  const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait(); value = {projects: []}; await view.data.refresh(); await wait();
    transport.emit(summary(99, 99), "id-one"); await wait();
    value = overview(summary(2, 0, 1), "id-two"); await view.data.refresh(); await wait();
    expect(view.data.projects[0].project_id).toBe("id-two");
    expect(view.data.projects[0].activeProcessCount).toBe(0);
    expect(view.data.projects[0].finishedProcessCount).toBe(1);
  } finally {view.close();}
});

for (const state of ["unavailable", "completed"] as const) test(`same-time ${state} event survives the older in-flight snapshot`, async () => {
  let value: any = overview(summary(10, 1));
  const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait();
    const pending = deferred(); value = pending.promise;
    const refreshing = view.data.refresh();
    const event = state === "unavailable"
      ? {...summary(10, 1), daemon_online: false, state_available: false,
        summary_error: "CURRENT_STORAGE_UNAVAILABLE", last_known_at: "10", stale: true}
      : summary(10, 0, 1);
    transport.emit(event); await wait();
    pending.resolve(overview(summary(10, 1))); await refreshing; await wait();
    if (state === "unavailable") {
      expect(view.data.projects[0].stateAvailable).toBe(false);
      expect(view.data.projects[0].stateError).toBe("CURRENT_STORAGE_UNAVAILABLE");
    } else {
      expect(view.data.projects[0].activeProcessCount).toBe(0);
      expect(view.data.projects[0].finishedProcessCount).toBe(1);
    }
  } finally {view.close();}
});

test("a genuinely newer Host snapshot can clear an unavailable event received during its request", async () => {
  const pending = deferred(), transport = backend(() => pending.promise), view = mount(transport.client);
  try {
    await wait();
    transport.emit({...summary(10, 1), daemon_online: false, state_available: false, summary_error: "OLD_STORAGE_ERROR"});
    pending.resolve(overview(summary(11, 0, 1))); await wait();
    expect(view.data.projects[0].stateAvailable).toBe(true); expect(view.data.projects[0].stateError).toBe("");
    expect(view.data.projects[0].finishedProcessCount).toBe(1);
  } finally {view.close();}
});

test("a same-time snapshot requested after an event can reconcile it instead of pinning it", async () => {
  let value: any = overview(summary(10, 1));
  const transport = backend(() => value), view = mount(transport.client);
  try {
    await wait();
    transport.emit({...summary(10, 1), daemon_online: false, state_available: false, summary_error: "OLD_STORAGE_ERROR"});
    await wait(); expect(view.data.projects[0].stateAvailable).toBe(false);
    value = overview(summary(10, 0, 1)); await view.data.refresh(); await wait();
    expect(view.data.projects[0].stateAvailable).toBe(true); expect(view.data.projects[0].stateError).toBe("");
    expect(view.data.projects[0].finishedProcessCount).toBe(1);
  } finally {view.close();}
});

for (const name of ["constructor", "__proto__", "toString"]) test(`project name ${name} retains cold and warm events across reconnect`, async () => {
  const pending = deferred(), transport = backend(() => pending.promise), view = mount(transport.client);
  const namedOverview = (time: number, identity: string) => {
    const value = overview(summary(time, 0), identity); value.projects[0].name = name; return value;
  };
  try {
    await wait();
    expect(() => transport.emit(summary(2, 3), "id-one", name)).not.toThrow();
    expect(() => transport.emit(summary(2, 4), undefined, name)).not.toThrow();
    pending.resolve(namedOverview(1, "id-one")); await wait();
    expect(view.data.projects[0].name).toBe(name); expect(view.data.projects[0].activeProcessCount).toBe(4);
    transport.emit(summary(3, 5), "id-one", name); await wait();
    expect(view.data.projects[0].activeProcessCount).toBe(5);
    const next = deferred(), fresh = backend(() => next.promise); await view.reconnect(fresh.client);
    transport.emit(summary(99, 99), "id-one", name);
    expect(() => fresh.emit(summary(4, 6), "id-two", name)).not.toThrow();
    next.resolve(namedOverview(3, "id-two")); await wait();
    expect(view.data.projects[0].project_id).toBe("id-two"); expect(view.data.projects[0].activeProcessCount).toBe(6);
  } finally {view.close();}
});
