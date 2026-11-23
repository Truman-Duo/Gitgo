import React,{useMemo} from "react";
import {Box,Text} from "@anthropic/ink";
import type {PendingDecision} from "../types.js";
import {colors} from "../theme/index.js";
import {useCopy} from "../ui/PageFrame.js";
import {projectRuntimeDecision} from "../ui/decisionCopy.js";

type AnswerQuestion=Pick<PendingDecision,"options">;
export function answeredDecisionView(decision:PendingDecision, answer:string):PendingDecision {
  if(decision.answer_mode!=="incremental" || !decision.questions?.length)return decision;
  try{
    const values=JSON.parse(answer)?.answers;
    if(!values || typeof values!=="object" || Array.isArray(values))return decision;
    const questions=decision.questions.filter(q=>Object.hasOwn(values,q.node_id));
    if(!questions.length)return decision;
    return {...decision,...(questions.length===1?questions[0]:{}),questions,
      question:questions.map(q=>q.question).join("\n")};
  }catch{return decision;}
}
function optionContent(answer:string,source?:AnswerQuestion,display=source):string {
  const selected=/^Choose option ([1-9]\d*): ([\s\S]*)$/.exec(answer);
  if(!selected || !source)return answer;
  const index=Number(selected[1])-1,option=source.options[index];
  if(!option)return answer;
  const recorded=selected[2]!,label=option.label;
  // This is the existing keyboard submission envelope, validated against its
  // original option. Never strip lookalike free text or lose added conditions.
  if(recorded!==label && !recorded.startsWith(label+"; conditions:"))return answer;
  return (display?.options[index]?.label ?? label)+recorded.slice(label.length);
}

export function formatRecordedAnswer(answer:string,decision?:PendingDecision,display=decision):string {
  try{
    const value=JSON.parse(answer);
    if(value && typeof value.answers==="object" && value.answers && !Array.isArray(value.answers)){
      const ordered=decision?.questions?.map(question=>question.node_id).filter(id=>Object.hasOwn(value.answers,id)) || [];
      const keys=[...ordered,...Object.keys(value.answers).filter(id=>!ordered.includes(id))];
      return keys.map(id=>{const item=value.answers[id];return typeof item==="string"
        ? optionContent(item,decision?.questions?.find(question=>question.node_id===id),display?.questions?.find(question=>question.node_id===id))
        : JSON.stringify(item);}).join("\n");
    }
  }catch{/* Ordinary user text remains verbatim. */}
  return optionContent(answer,decision,display);
}

/** The same frame is used by the concise transcript card and full decision. */
export function DecisionCardFrame({children}: {children:React.ReactNode}) {
  return <Box flexDirection="column" minWidth={0} borderStyle="single" borderColor={colors.divider.color}
    paddingLeft={1} paddingRight={1} marginTop={1} marginBottom={1}>{children}</Box>;
}

export const DecisionSummaryText=React.memo(function DecisionSummaryText({decision,answer,layout="inline",marker=""}: {
  decision:PendingDecision;answer:string;layout?:"inline"|"stacked";marker?:string;
}) {
  const t=useCopy();
  const {question,reply}=useMemo(()=>{
    const display=projectRuntimeDecision(answeredDecisionView(decision,answer),t);
    return {question:display.questions?.length && display.questions.length>1
      ? display.questions.map(item=>item.question).join("\n") : display.question,
      reply:formatRecordedAnswer(answer,decision,display)};
  },[decision,answer,t]);
  if(layout==="stacked")return <Box flexDirection="column" minWidth={0}>
    <Text bold>{question}{marker ? " "+marker : ""}</Text>
    <Text color={colors.chat.userMarker}>{"› "}{reply}</Text>
  </Box>;
  return <Box flexDirection="row" minWidth={0} height={1} flexShrink={0} overflow="hidden">
    {marker ? <Box flexShrink={0}><Text bold>{marker+" "}</Text></Box> : null}
    <Box flexShrink={1} minWidth={0}><Text bold wrap="truncate-end">{question.replace(/\r?\n/g," · ")}</Text></Box>
    <Box flexShrink={0}><Text color={colors.chat.userMarker}>{" › "}</Text></Box>
    <Box maxWidth="50%" flexShrink={0} minWidth={0}><Text wrap="truncate-end" color={colors.chat.userMarker}>{reply.replace(/\r?\n/g," · ")}</Text></Box>
  </Box>;
});

/** Read-only user input remains visible when the process is folded. This
 * component has no submit handler or permission/task lifecycle authority. */
export function DecisionSummary({decision,answer}: {decision:PendingDecision;answer:string}) {
  return <DecisionCardFrame><DecisionSummaryText decision={decision} answer={answer}/></DecisionCardFrame>;
}
