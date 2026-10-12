import {expect, test} from "bun:test";
import {answerRound, projectRound, previousRound} from "./decisionRound.js";
import type {PendingDecision} from "../types.js";

const round: PendingDecision = {task_id: "t", process_id: "p", decision_id: "d", question: "Round", why_user_must_decide: "Intent", options: [], allow_free_form: true,
  questions: ["scope", "audience"].map(node_id => ({node_id, question: node_id, why_user_must_decide: "Intent", kind: "clarification", state_topic: node_id, options: [], allow_free_form: true}))};

test("round drafts cannot send a partial answer to Host", () => {
  expect(projectRound(round, {})?.question).toBe("scope");
  const first = answerRound(round, {}, "small");
  expect(first.instruction).toBeNull();
  expect(projectRound(round, first.answers)?.round_position).toBe("2/2");
  const last = answerRound(round, first.answers, "teams");
  expect(JSON.parse(last.instruction!).answers).toEqual({scope: "small", audience: "teams"});
  expect(projectRound(round, first.answers)?.decision_id).toBe("d");
  expect(projectRound(round, previousRound(round, first.answers))?.question).toBe("scope");
});

test("ordinary permission cards retain the original identity and answer", () => {
  const permission = {...round, kind: "permission", questions: undefined};
  expect(projectRound(permission, {})).toBe(permission);
  expect(answerRound(permission, {}, "Choose option 1: Allow").instruction).toBe("Choose option 1: Allow");
});

test("single Host question submits immediately, including autonomy instructions", () => {
  const current = {...round, questions: [round.questions![0]!]};
  const reply = "个人离线 Markdown 阅读器，其余自行决定，不要再问";
  const next = answerRound(current, {}, reply);
  expect(JSON.parse(next.instruction!).answers).toEqual({scope: reply});
  expect(projectRound(current, {})?.round_position).toBeUndefined();
});
