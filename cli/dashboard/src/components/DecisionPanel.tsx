import {SelectionText} from "./Selection.js";
import {applicationBindings} from "../input/router.js";
import {useCopy} from "../ui/PageFrame.js";
import React, { useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { PendingDecision } from "../types.js";
import { AnimatedReveal } from "./AnimatedReveal.js";
import {projectRuntimeDecision} from "../ui/decisionCopy.js";
import {DecisionCardFrame,DecisionSummaryText,answeredDecisionView} from "./DecisionSummary.js";
import type {ExpandedChange} from "../hooks/useDisclosureState.js";
import {DetailViewport} from "./DetailViewport.js";

type Props = {
  decision: PendingDecision;
  selectedIndex: number;
  composing: boolean;
  submitting: boolean;
  active?: boolean;
  answer?: string | null;
  expanded?: boolean;
  onExpandedChange?: (value: ExpandedChange) => void;
};

export function DecisionPanel({
  decision: sourceDecision,
  selectedIndex,
  composing,
  submitting,
  active = true,
  answer,
  expanded: controlledExpanded,
  onExpandedChange,
}: Props) {
  const t=useCopy();
  const decision=projectRuntimeDecision(!active&&answer?answeredDecisionView(sourceDecision,answer):sourceDecision,t);
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlledExpanded ?? localExpanded;
  const changeExpanded = (value:ExpandedChange) => {
    if (controlledExpanded === undefined) setLocalExpanded(value);
    onExpandedChange?.(value);
  };
  const toggle=()=>changeExpanded(current=>!current);
  const showOptions = active || expanded;
  const permission = decision.kind === "permission";
  const manual = decision.kind === "manual_execution";
  const answered=!active && Boolean(answer);
  const independentGroup=active && decision.answer_mode==="incremental" && (decision.questions?.length || 0)>1;
  return (
    <DecisionCardFrame>
      <Box onClick={toggle} clickMode="action" flexDirection="column" minWidth={0}>
        {answered ? <DecisionSummaryText decision={sourceDecision} answer={answer!}
          layout="inline" marker={expanded ? "▾" : "▸"}/> : <Text bold>
          {!active ? (expanded ? "▾ " : "▸ ") : ""}{active
            ? manual ? t({en:"Manual · next step",zh:"Manual · 下一步"}) : decision.kind === "permission"
              ? t({en:"Permission required",zh:"需要授权"})
              : t({en:"Information requested",zh:"需要补充信息"})
            : t({en:"Decision",zh:"待确认事项"})}
        </Text>}
      </Box>
      <AnimatedReveal expanded={showOptions}>
       <DetailViewport enabled={!active} framed={false} onClose={()=>changeExpanded(false)}>
        {answered && expanded ? <DecisionSummaryText decision={sourceDecision} answer={answer!} layout="stacked"/> : null}
        {independentGroup ? decision.questions!.map(q=><Box key={q.node_id} flexDirection="column" marginBottom={1}>
          <SelectionText selected={q.node_id===(decision.active_question_id || decision.questions![0]!.node_id)} variant="block">{q.question}</SelectionText>
          {q.recommendation?<Text dimColor>{t({en:"Suggestion: ",zh:"建议："})}{q.recommendation}</Text>:null}
        </Box>) : !answered ? <Text bold>
          {decision.round_position ? `${decision.round_position} · ` : ""}
          {manual ? t({en:"Run the next work unit?",zh:"执行下一步工作？"}) : decision.question}
        </Text> : null}
        {!answered && !independentGroup && decision.questions &&
        decision.questions.length > 1 &&
        !decision.round_position
          ? decision.questions.map((q) => (
              <Text key={q.node_id}>{q.question}</Text>
            ))
          : null}
        {decision.materials?.length ? <Box flexDirection="column" marginTop={1}>
          <Text dimColor>{t({en:"Supplementary material",zh:"补充材料"})}</Text>
          {decision.materials.map(material => <Text key={material.reference}>{material.summary}</Text>)}
        </Box> : null}

        <Text dimColor>
          {permission ? t({en:"Why approval is needed",zh:"为何需要授权"}) : manual ? t({en:"Advancement",zh:"推进方式"}) : t({en:"Information needed",zh:"需要补充的信息"})}:{" "}
          {manual ? t({en:"Manual pauses before this step; tool and sandbox permissions still apply.",zh:"Manual 在本步前等待确认；工具权限与沙箱限制仍然生效。"}) : decision.why_user_must_decide}
        </Text>
        {!independentGroup && decision.recommendation?<Text dimColor>{t({en:"Suggestion: ",zh:"建议："})}{decision.recommendation}</Text>:null}
        {manual && decision.manual_unit ? <Box flexDirection="column">
          <Text>{decision.manual_unit.tool_name}</Text>
          <Text dimColor>{JSON.stringify(decision.manual_unit.arguments, null, 2).slice(0, 1200)}
            {JSON.stringify(decision.manual_unit.arguments, null, 2).length > 1200 ? "…" : ""}</Text>
        </Box> : null}
        {decision.permission_request ? (
          <Box flexDirection="column">
            <Text>{t({en:"Purpose: ",zh:"用途："})}{decision.permission_request.purpose}</Text>
            <Text>{t({en:"Target: ",zh:"目标："})}{decision.permission_request.resource}</Text>
            {expanded ? (
              <>
                <Text dimColor>
                  {t({en:"Effect: ",zh:"行为："})}{decision.permission_request.effect}
                </Text>
                {decision.permission_request.arguments_preview ? (
                  <Text dimColor>
                    {t({en:"Invocation: ",zh:"调用："})}{decision.permission_request.arguments_preview}
                  </Text>
                ) : null}
                <Text dimColor>
                  {t({en:"Technical details: ",zh:"技术详情："})}{decision.permission_request.api_details}
                </Text>
              </>
            ) : null}
          </Box>
        ) : null}
        <Box flexDirection="column" marginTop={1}>
          {decision.options.map((option, index) => {
            const selected = active && index === selectedIndex;
            return (
              <Box
                key={`${decision.decision_id}:${index}`}
                flexDirection="column"
                marginBottom={1}
              >
                <SelectionText selected={selected} variant="block">
                  {index + 1}. {active && index<9 && applicationBindings.label("decision.option"+(index+1))!==String(index+1) ? "["+applicationBindings.label("decision.option"+(index+1))+"] " : ""}{manual ? index === 0 ? t({en:"Run once",zh:"执行这一步"}) : t({en:"Skip this invocation",zh:"跳过本次调用"}) : option.label}
                  {option.recommended ? t({en:" · recommended",zh:" · 建议"}) : ""}
                </SelectionText>
                {manual ? null : permission ? (
                  <>
                    <Text> {t({en:"Effect: ",zh:"行为："})}{option.immediate_effect}</Text>
                    {option.risks ? <Text> {t({en:"Risk: ",zh:"风险："})}{option.risks}</Text> : null}
                    {expanded ? (
                      <>
                        <Text dimColor> {t({en:"Principle: ",zh:"依据："})}{option.principle}</Text>
                        <Text dimColor> {t({en:"Then: ",zh:"后续："})}{option.downstream_effect}</Text>
                        <Text dimColor>
                          {" "}
                          {t({en:"Reversibility: ",zh:"可逆性："})}{option.reversibility}
                        </Text>
                      </>
                    ) : null}
                  </>
                ) : decision.kind === "clarification" ? (
                  <>
                  <Text dimColor>
                    {" "}
                    {option.description || option.immediate_effect}
                  </Text>
                  {answered && expanded ? <>
                    {option.description && option.immediate_effect && option.description!==option.immediate_effect
                      ? <Text> {t({en:"Effect: ",zh:"行为："})}{option.immediate_effect}</Text> : null}
                    {option.principle ? <Text dimColor> {t({en:"Principle: ",zh:"依据："})}{option.principle}</Text> : null}
                    {option.downstream_effect ? <Text dimColor> {t({en:"Then: ",zh:"后续："})}{option.downstream_effect}</Text> : null}
                    {option.risks ? <Text> {t({en:"Risk: ",zh:"风险："})}{option.risks}</Text> : null}
                    {option.reversibility ? <Text dimColor> {t({en:"Reversibility: ",zh:"可逆性："})}{option.reversibility}</Text> : null}
                  </> : null}
                  </>
                ) : (
                  <>
                    <Text>{option.description || option.immediate_effect}</Text>
                    {option.risks ? <Text> {t({en:"Risk: ",zh:"风险："})}{option.risks}</Text> : null}
                    {expanded ? <>
                      <Text dimColor> {t({en:"Principle: ",zh:"依据："})}{option.principle}</Text>
                      <Text dimColor> {t({en:"Downstream: ",zh:"后续影响："})}{option.downstream_effect}</Text>
                      <Text dimColor> {t({en:"Reversibility: ",zh:"可逆性："})}{option.reversibility}</Text>
                    </> : null}
                  </>
                )}
              </Box>
            );
          })}
          {active&&decision.allow_free_form?<SelectionText selected={selectedIndex===decision.options.length} variant="block"

            >
            {t({en:"Other / direct reply",zh:"其他 / 直接输入"})}
          </SelectionText>:null}
        </Box>
       </DetailViewport>
      </AnimatedReveal>
      {active && submitting ? (
        <Text dimColor>{t({en:"Submitting the reply…",zh:"正在提交答复…"})}</Text>
      ) : null}
    </DecisionCardFrame>
  );
}
