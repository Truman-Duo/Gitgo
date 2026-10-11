import {expect, test} from "bun:test";
import {pendingDecisionFromOutcome} from "./sendChat.js";
import {projectRound, answerRound} from "../hooks/decisionRound.js";

test("live TaskOutcome retains question identity and produces a complete Host answer round", () => {
  const raw = {decision_id: "real-card", process_id: "p", task_id: "t", kind: "clarification",
    question: "Questions", options: [], allow_free_form: true, questions: [
      {node_id: "audience", question: "For whom?", options: [], allow_free_form: true},
      {node_id: "scope", question: "What problem?", options: [], allow_free_form: true},
    ]};
  const decision = pendingDecisionFromOutcome({metadata: {pending_decision: raw}})!;
  expect(projectRound(decision, {})?.question).toBe("For whom?");
  const first = answerRound(decision, {}, "my team");
  expect(first.instruction).toBeNull();
  expect(projectRound(decision, first.answers)?.question).toBe("What problem?");
  const second = answerRound(decision, first.answers, "track deadlines");
  expect(JSON.parse(second.instruction!)).toEqual({answers: {audience: "my team", scope: "track deadlines"}});
  expect(decision.decision_id).toBe("real-card");
});
