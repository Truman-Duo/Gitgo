import {useCopy} from "../ui/PageFrame.js";
import React, { useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { PendingDecision } from "../types.js";
import { AnimatedReveal } from "./AnimatedReveal.js";
import { colors } from "../theme/index.js";

type Props = {
  decision: PendingDecision;
  selectedIndex: number;
  composing: boolean;
  submitting: boolean;
  active?: boolean;
  answer?: string | null;
  expanded?: boolean;
  onExpandedChange?: (value: boolean) => void;
};

export function DecisionPanel({
  decision,
  selectedIndex,
  composing,
  submitting,
  active = true,
  answer,
  expanded: controlledExpanded,
  onExpandedChange,
}: Props) {
  const t=useCopy();
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlledExpanded ?? localExpanded;
  const toggle = () => {
    if (controlledExpanded === undefined) setLocalExpanded(!expanded);
    onExpandedChange?.(!expanded);
  };
  const showOptions = active || expanded;
  const permission = decision.kind === "permission";
  return (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor={colors.divider.color}
      paddingLeft={1}
      paddingRight={1}
      marginTop={1}
      marginBottom={1}
    >
      <Box onClick={toggle}>
        <Text bold>
          {active
            ? decision.kind === "permission"
              ? t({en:"Permission required",zh:"需要授权"})
              : t({en:"Information requested",zh:"需要补充信息"})
            : answer
              ? t({en:"Decision answered",zh:"已答复"})
              : t({en:"Decision",zh:"待确认事项"})}
          {!active ? (expanded ? " ▾" : " ▸") : ""}
        </Text>
      </Box>
      <AnimatedReveal expanded={showOptions}>
        <Text bold>
          {decision.round_position ? `${decision.round_position} · ` : ""}
          {decision.question}
        </Text>
        {decision.questions &&
        decision.questions.length > 1 &&
        !decision.round_position
          ? decision.questions.map((q) => (
              <Text key={q.node_id}>{q.question}</Text>
            ))
          : null}
        {answer ? <Text>{formatAnswer(answer)}</Text> : null}

        <Text dimColor>
          {permission ? t({en:"Why approval is needed",zh:"为何需要授权"}) : t({en:"Information needed",zh:"需要补充的信息"})}:{" "}
          {decision.why_user_must_decide}
        </Text>
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
            const bg = selected ? colors.selection.block.bg : undefined;
            const fg = selected ? colors.selection.block.fg : undefined;
            return (
              <Box
                key={`${decision.decision_id}:${option.label}`}
                flexDirection="column"
                marginBottom={1}
              >
                <Text color={fg} backgroundColor={bg} bold={selected}>
                  {selected ? "› " : "  "}
                  {index + 1}. {option.label}
                  {option.recommended ? t({en:" · recommended",zh:" · 建议"}) : ""}
                </Text>
                {permission ? (
                  <>
                    <Text> {t({en:"Effect: ",zh:"行为："})}{option.immediate_effect}</Text>
                    <Text dimColor> {t({en:"Then: ",zh:"后续："})}{option.downstream_effect}</Text>
                    {expanded ? (
                      <>
                        <Text dimColor> {t({en:"Principle: ",zh:"依据："})}{option.principle}</Text>
                        <Text> {t({en:"Risk: ",zh:"风险："})}{option.risks}</Text>
                        <Text dimColor>
                          {" "}
                          {t({en:"Reversibility: ",zh:"可逆性："})}{option.reversibility}
                        </Text>
                      </>
                    ) : null}
                  </>
                ) : decision.kind === "clarification" ? (
                  <Text dimColor>
                    {" "}
                    {option.description || option.immediate_effect}
                  </Text>
                ) : (
                  <>
                    <Text dimColor> {t({en:"Principle: ",zh:"依据："})}{option.principle}</Text>
                    <Text> {t({en:"Immediate: ",zh:"当前影响："})}{option.immediate_effect}</Text>
                    <Text> {t({en:"Downstream: ",zh:"后续影响："})}{option.downstream_effect}</Text>
                    <Text> {t({en:"Risk: ",zh:"风险："})}{option.risks}</Text>
                    <Text dimColor> {t({en:"Reversibility: ",zh:"可逆性："})}{option.reversibility}</Text>
                  </>
                )}
              </Box>
            );
          })}
          {active&&decision.allow_free_form?<Text
            backgroundColor={selectedIndex===decision.options.length?colors.selection.block.bg:undefined}
            color={selectedIndex===decision.options.length?colors.selection.block.fg:undefined}>
            {selectedIndex===decision.options.length?"› ":"  "}{t({en:"Other / direct reply",zh:"其他 / 直接输入"})}
          </Text>:null}
        </Box>
      </AnimatedReveal>
      {active ? (
        <Text dimColor>
          {decision.round_position && !decision.round_position.startsWith("1/")
            ? t({en:"← previous question · ",zh:"← 上一题 · "})
            : ""}
          {submitting
            ? t({en:"Submitting the reply…",zh:"正在提交答复…"})
            : composing
              ? t({en:"Enter send · Esc choices",zh:"Enter 发送 · Esc 返回选项"})
              : decision.options.length > 0
                ? t({en:"↑/↓ select · Enter choose/edit",zh:"↑/↓ 选择 · Enter 确认/输入"}) + (decision.allow_free_form ? t({en:" · A amend · D discuss · Other: direct reply",zh:" · A 补充 · D 讨论 · 其他：直接输入"}) : "")
                : t({en:"Type your reply · Enter send",zh:"直接输入答复 · Enter 发送"})}
        </Text>
      ) : null}
    </Box>
  );
}

function formatAnswer(answer: string) {
  try {
    const value = JSON.parse(answer);
    if (value.answers) return Object.values(value.answers).join("\n");
  } catch {
    /* ordinary text */
  }
  return answer;
}
