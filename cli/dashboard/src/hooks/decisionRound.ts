import type {PendingDecision} from "../types.js";

/** UI drafts are never approvals. Only the final complete round goes to Host. */
export function projectRound(decision: PendingDecision | null, answers: Record<string, string>): PendingDecision | null {
  if (!decision?.questions?.length) return decision;
  const index = decision.questions.findIndex(q => !answers[q.node_id]);
  const question = decision.questions[index < 0 ? decision.questions.length - 1 : index]!;
  return {...decision, ...question, round_position: decision.questions.length > 1
    ? `${(index < 0 ? decision.questions.length - 1 : index) + 1}/${decision.questions.length}` : undefined};
}

export function answerRound(decision: PendingDecision, answers: Record<string, string>, text: string) {
  const question = decision.questions?.find(q => !answers[q.node_id]);
  if (!question) return {answers, instruction: text};
  const next = {...answers, [question.node_id]: text.trim()};
  return {answers: next, instruction: decision.questions!.every(q => next[q.node_id]) ? JSON.stringify({answers: next}) : null};
}

export function previousRound(decision: PendingDecision, answers: Record<string, string>) {
  const previous = [...(decision.questions || [])].reverse().find(q => answers[q.node_id]);
  if (!previous) return answers;
  const next = {...answers};
  delete next[previous.node_id];
  return next;
}
