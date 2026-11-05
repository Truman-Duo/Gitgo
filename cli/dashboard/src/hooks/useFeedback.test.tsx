import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync} from "@anthropic/ink";
import {useFeedback, type FeedbackScope} from "./useFeedback.js";
import {FeedbackMessage} from "../components/FeedbackMessage.js";
import {colors} from "../theme/index.js";

const wait = (ms = 35) => new Promise(resolve => setTimeout(resolve, ms));
function mount(scope: FeedbackScope) {
  const stdin: any = new PassThrough(); Object.assign(stdin, {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stdout: any = new PassThrough(); Object.assign(stdout, {isTTY: false, columns: 80, rows: 10}); stdout.on("data", () => {});
  const stderr: any = new PassThrough(); stderr.on("data", () => {});
  let latest!: ReturnType<typeof useFeedback>;
  function Harness({scope}: {scope: FeedbackScope}) {latest = useFeedback(scope); return <FeedbackMessage feedback={latest.message}/>;}
  const root = renderSync(<Harness scope={scope}/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  return {get feedback() {return latest;}, async page(next: FeedbackScope) {root.rerender(<Harness scope={next}/>); await wait();},
    close() {root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy();}};
}

test("page feedback is cleared on navigation and cannot return or arrive late", async () => {
  const io = mount({kind: "page", id: "workspace"});
  try {
    io.feedback.show("Needs a parent baseline", {tone: "warning"}); await wait();
    const late = io.feedback.show;
    expect(io.feedback.text).toBe("Needs a parent baseline");
    await io.page({kind: "page", id: "collaboration"}); expect(io.feedback.text).toBe("");
    late("Old request failed"); await wait(); expect(io.feedback.text).toBe("");
    await io.page({kind: "page", id: "workspace"}); expect(io.feedback.text).toBe("");
  } finally {io.close();}
});

test("feedback durations are finite by default and an old expiry cannot clear a newer result", async () => {
  const io = mount({kind: "page", id: "settings"});
  try {
    const before = Date.now(); io.feedback.show("Failed", {tone: "error"}); await wait();
    expect(io.feedback.message!.expiresAt).toBeGreaterThanOrEqual(before + colors.feedback.errorMs);
    io.feedback.show("First", {retention: {kind: "timed", durationMs: 80}}); await wait();
    io.feedback.show("Second", {retention: {kind: "timed", durationMs: 160}}); await wait(90);
    expect(io.feedback.text).toBe("Second"); await wait(100); expect(io.feedback.text).toBe("");
  } finally {io.close();}
});

test("cross-page feedback requires a project/global owner and persistence is explicitly unresolved", async () => {
  const io = mount({kind: "project", id: "one"});
  try {
    io.feedback.show("Checking", {retention: {kind: "until-resolved", reason: "operation-pending"}}); await wait();
    await io.page({kind: "project", id: "one"}); expect(io.feedback.text).toBe("Checking");
    expect(io.feedback.message!.expiresAt).toBeUndefined();
    io.feedback.clear(); await wait(); expect(io.feedback.text).toBe("");
    io.feedback.show("One only"); await wait();
    await io.page({kind: "project", id: "two"}); expect(io.feedback.text).toBe("");
  } finally {io.close();}
});
