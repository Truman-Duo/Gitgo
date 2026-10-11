import React from "react";
import {expect, test} from "bun:test";
import {PassThrough} from "node:stream";
import {renderSync, Text} from "@anthropic/ink";
import {NativeHostClient} from "../backend/client.js";
import {useChat, reconcileChatMessages} from "./useChat.js";

test("a rejected round keeps its active card; transport acceptance cannot mark it answered", async () => {
  const decision: any = {decision_id: "card", task_id: "task", process_id: "process",
    question: "Question", kind: "clarification", options: [], allow_free_form: true,
    questions: [{node_id: "need", question: "What do you need?", options: [], allow_free_form: true}]};
  let hook: ReturnType<typeof useChat>;
  let admitted = false;
  class Client extends NativeHostClient {
    get ready() {return true;}
    async sendTaskStreaming(_args: any, callbacks: any) {
      callbacks.onAcknowledged({stage: "accepted", request_id: "transport"});
      await new Promise(resolve => setTimeout(resolve, 20));
      expect(hook!.pendingDecision?.decision_id).toBe("card");
      expect(hook!.messages.find(row => row.decision)?.decision_answer).toBeUndefined();
      if (!admitted) {callbacks.onError(new Error("DECISION_ROUND_INVALID")); return;}
      callbacks.onAcknowledged({stage: "admitted", request_id: "transport", process_id: "process", task_id: "task"});
      callbacks.onComplete({outcome: {task_id: "task", process_id: "process", status: "completed", response: "Adopted"}});
    }
  }
  const client = new Client("unused", "unused");
  const seed: any[] = [{message_id: "decision:card", role: "assistant", content: "Question", decision, status: "awaiting_user"}];
  function Probe() {hook = useChat(client, "project", undefined, seed, decision); return <Text>{hook.pendingDecision?.question || "done"}</Text>;}
  const stdin: any = new PassThrough(); stdin.isTTY = false;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 24;
  stdout.on("data", () => {});
  const instance = renderSync(<Probe />, {stdin, stdout, stderr: stdout, patchConsole: false, exitOnCtrlC: false});
  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    await hook!.send("not a valid round");
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(hook!.pendingDecision?.decision_id).toBe("card");
    expect(hook!.messages.find(row => row.decision)?.decision_answer).toBeUndefined();
    expect(hook!.messages.some(row => row.content.includes("DECISION_ROUND_INVALID"))).toBe(true);
    expect(reconcileChatMessages(seed, hook!.messages).some(row => row.content.includes("DECISION_ROUND_INVALID"))).toBe(true);
    admitted = true;
    await hook!.send(JSON.stringify({answers: {need: "markdown reader"}}));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(hook!.pendingDecision).toBeNull();
    expect(hook!.messages.find(row => row.decision)?.status).toBe("answered");
    expect(hook!.messages.some(row => row.content === "Adopted")).toBe(true);
  } finally {instance.unmount(); instance.cleanup(); stdin.destroy(); stdout.destroy();}
});
