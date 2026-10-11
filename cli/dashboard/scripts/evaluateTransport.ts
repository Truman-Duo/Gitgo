/** Real frontend transport/API check; never represents visual acceptance. */
import {writeFileSync} from "node:fs";
import {NativeHostClient} from "../src/backend/client.js";
import {sendChat} from "../src/chat/sendChat.js";
import {answerRound, projectRound} from "../src/hooks/decisionRound.js";
import type {PendingDecision} from "../src/types.js";

const profile = process.env.GITGO_ACCEPTANCE_PROFILE!;
const client = new NativeHostClient(process.env.GITGO_PYTHON!, process.env.GITGO_ROOT!, "");
const result: any = {visual_dashboard_verified: false, rounds: [], passed: false};
const save = () => writeFileSync(`${profile}/frontend-result.json`, JSON.stringify(result, null, 2));
let decision: PendingDecision | null = null;
let last = "";
async function send(text: string, current: PendingDecision | null) {
  const row: any = {acks: [], submitted_decision_id: current?.decision_id};
  result.rounds.push(row);
  await sendChat(client, "2920", text, new Date().toISOString(), current, {
    onAcknowledged(ack) {row.acks.push(ack);},
    onStream() {},
    onDone(content, _tools, _notices, pending, round) {
      last = content; decision = pending || null;
      row.response = content; row.pending = decision; row.status = round?.status;
    },
    onError(message) {row.error = message;},
  });
  save();
  return row;
}
try {
  await client.start();
  let row = await send("我想做一个小工具，但还没决定给谁用，你先帮我明确需求。先讨论，不要写代码或文件。", null);
  if (row.error || !decision?.decision_id || !decision.question) throw new Error("Expected a genuine Host question");
  if (decision.questions && decision.questions.length !== 1) throw new Error("Host must publish one facet before adopting its answer");
  result.card_shape = decision.questions?.length ? "round" : "single";
  const original = decision;
  // A malformed text answer must remain visible as an error, with the same
  // durable card and no admitted acknowledgement or fake answered state.
  if (original.questions?.length) {
    row = await send("你想说什么", original);
    if (!row.error?.includes("DECISION_ROUND_INVALID") || row.acks.some((ack: any) => ack.stage === "admitted"))
      throw new Error("Rejected answer was accepted by the frontend transport");
    const status = await client.callTool("runtime.status", {project: "2920"});
    const saved = status.processes[original.process_id]?.pending_decision;
    if (saved?.decision_id !== original.decision_id) throw new Error("Rejected answer lost the durable card");
    result.round_validation_verified = true;
  }
  decision = original;
  for (let round = 0; round < 1 && decision; round++) {
    const current = decision;
    let answers: Record<string, string> = {};
    let instruction: string | null = null;
    if (!current.questions?.length) {
      const view = projectRound(current, answers)!;
      if (view.question !== current.question) throw new Error("Single question body lost");
      instruction = answerRound(current, answers,
        "个人离线 Markdown 阅读器，只给方案，其余自行决定，不要再提问。").instruction;
    }
    for (const question of current.questions || []) {
      const view = projectRound(current, answers)!;
      if (!view.question || view.question !== question.question) throw new Error("Live question body/identity lost");
      const next = answerRound(current, answers,
        "做一个给我个人在 Windows 上离线使用的 Markdown 阅读器，只读本地文件，不修改文件、不联网。现在仅给最小需求和架构方案，不实施；剩下的细节你自行选择默认值，不要再提问。");
      answers = next.answers; instruction = next.instruction;
    }
    if (!instruction) throw new Error("Missing complete answer payload");
    row = await send(instruction, current);
    if (row.error) throw new Error(row.error);
  }
  if (decision || row.status !== "completed" || !/markdown/i.test(last)) throw new Error("First answer/autonomy was not adopted before the next question");
  let duplicateRejected = false;
  try {
    await client.callTool("runtime.decision", {project: "2920", task_id: original.task_id,
      process_id: original.process_id, decision_id: original.decision_id, message: "Repeated real answer"});
  } catch {duplicateRejected = true;}
  if (!duplicateRejected) throw new Error("Already answered real identity was accepted twice");
  result.duplicate_reply_rejected = true;
  result.passed = true;
} catch (error) {
  result.error = String(error);
} finally {
  save(); await client.close();
}
console.log(JSON.stringify({passed: result.passed, rounds: result.rounds.map((r: any) => ({status: r.status, error: r.error, questions: r.pending?.questions?.length})), visual_dashboard_verified: false}));
process.exit(result.passed ? 0 : 1);
