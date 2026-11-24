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

test('live transport retains immediate answer mode, advice and compact option descriptions',()=>{
 const decision=pendingDecisionFromOutcome({metadata:{pending_decision:{decision_id:'q',process_id:'p',task_id:'t',
  question:'Which policy?',why_user_must_decide:'Business choice',recommendation:'Keep both for audit.',answer_mode:'incremental',
  options:[{label:'Keep both',description:'Preserves records',recommended:true}],allow_free_form:true,
  questions:[{node_id:'policy',question:'Which policy?',recommendation:'Keep both for audit.',state_topic:'policy',options:[]}]}}})!;
 expect(decision.answer_mode).toBe('incremental');expect(decision.recommendation).toBe('Keep both for audit.');
 expect(decision.options[0]!.description).toBe('Preserves records');
 expect(JSON.parse(answerRound(decision,{},'Keep both').instruction!)).toEqual({answers:{policy:'Keep both'}});
});
