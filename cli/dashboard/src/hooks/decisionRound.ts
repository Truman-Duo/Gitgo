import type {PendingDecision} from "../types.js";

/** Selection is UI state; every new-round reply is submitted to Runtime now. */
export function projectRound(decision: PendingDecision | null, answers: Record<string, string>, activeId?:string): PendingDecision | null {
  if (!decision?.questions?.length) return decision;
  if(decision.answer_mode==="incremental"){
    const question=decision.questions.find(q=>q.node_id===activeId) || decision.questions[0]!;
    return {...decision,...question,active_question_id:question.node_id,round_position:undefined};
  }
  const index = decision.questions.findIndex(q => !answers[q.node_id]);
  const question = decision.questions[index < 0 ? decision.questions.length - 1 : index]!;
  return {...decision, ...question, round_position: decision.questions.length > 1
    ? `${(index < 0 ? decision.questions.length - 1 : index) + 1}/${decision.questions.length}` : undefined};
}

export function answerRound(decision: PendingDecision, answers: Record<string, string>, text: string, activeId?:string) {
  if(decision.answer_mode==="incremental" && decision.questions?.length){
    const question=decision.questions.find(q=>q.node_id===activeId) || decision.questions[0]!;
    return {answers:{},instruction:JSON.stringify({answers:{[question.node_id]:text.trim()}})};
  }
  const question = decision.questions?.find(q => !answers[q.node_id]);
  if (!question) return {answers, instruction: text};
  const next = {...answers, [question.node_id]: text.trim()};
  return {answers: next, instruction: decision.questions!.every(q => next[q.node_id]) ? JSON.stringify({answers: next}) : null};
}

export function moveRoundQuestion(decision:PendingDecision, activeId:string|undefined, delta:number):string|undefined {
  const questions=decision.questions || [];
  const index=Math.max(0,questions.findIndex(q=>q.node_id===activeId));
  return questions[Math.max(0,Math.min(questions.length-1,index+delta))]?.node_id;
}

export function previousRound(decision: PendingDecision, answers: Record<string, string>) {
  const previous = [...(decision.questions || [])].reverse().find(q => answers[q.node_id]);
  if (!previous) return answers;
  const next = {...answers};
  delete next[previous.node_id];
  return next;
}
