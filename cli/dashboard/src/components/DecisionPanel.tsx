import React, { useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { PendingDecision } from "../types.js";
import { colors } from "../theme/index.js";

type Props = {
  decision: PendingDecision;
  selectedIndex: number;
  composing: boolean;
  submitting: boolean;
  active?: boolean;
  answer?: string | null;
};

export function DecisionPanel({ decision, selectedIndex, composing, submitting, active = true, answer }: Props) {
  const [expanded, setExpanded] = useState(false);
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
      <Box onClick={() => setExpanded(value => !value)}>
        <Text bold>{active
          ? decision.kind === "permission" ? "Permission required" : `${decision.kind || "decision"} required`
          : answer ? "Decision answered" : "Decision"}{!active ? expanded ? " ▾" : " ▸" : ""}</Text>
      </Box>
      <Text bold>{decision.question}</Text>
      {answer ? <Text>{answer}</Text> : null}
      {showOptions ? <>
      <Text dimColor>{permission ? "Why approval is needed" : "Why only you can decide"}: {decision.why_user_must_decide}</Text>
      {decision.permission_request ? <Box flexDirection="column">
        <Text>Purpose: {decision.permission_request.purpose}</Text>
        <Text>Target: {decision.permission_request.resource}</Text>
        {expanded ? <>
          <Text dimColor>Effect: {decision.permission_request.effect}</Text>
          {decision.permission_request.arguments_preview
            ? <Text dimColor>Invocation: {decision.permission_request.arguments_preview}</Text>
            : null}
          <Text dimColor>Technical details: {decision.permission_request.api_details}</Text>
        </> : null}
      </Box> : null}
      <Box flexDirection="column" marginTop={1}>
        {decision.options.map((option, index) => {
          const selected = active && index === selectedIndex;
          const bg = selected ? colors.selection.block.bg : undefined;
          const fg = selected ? colors.selection.block.fg : undefined;
          return (
          <Box key={`${decision.decision_id}:${option.label}`} flexDirection="column" marginBottom={1}>
            <Text color={fg} backgroundColor={bg} bold={selected}>
              {selected ? "› " : "  "}{index + 1}. {option.label}{option.recommended ? " · recommended" : ""}
            </Text>
            {permission ? <>
              <Text>   Effect: {option.immediate_effect}</Text>
              <Text dimColor>   Then: {option.downstream_effect}</Text>
              {expanded ? <>
                <Text dimColor>   Principle: {option.principle}</Text>
                <Text>   Risk: {option.risks}</Text>
                <Text dimColor>   Reversibility: {option.reversibility}</Text>
              </> : null}
            </> : <>
              <Text dimColor>   Principle: {option.principle}</Text>
              <Text>   Immediate: {option.immediate_effect}</Text>
              <Text>   Downstream: {option.downstream_effect}</Text>
              <Text>   Risk: {option.risks}</Text>
              <Text dimColor>   Reversibility: {option.reversibility}</Text>
            </>}
          </Box>
          );
        })}
      </Box>
      </> : null}
      {active ? <Text dimColor>
        {submitting
          ? "Submitting the selected decision…"
          : composing
          ? "Editing decision reply in the NORMAL bar · Enter send · Esc return to choices"
          : decision.options.length > 0
          ? `↑↓ or 1-${decision.options.length} select · Enter confirm${decision.allow_free_form ? " · A amend · D discuss" : ""}`
          : "Type your reply in the NORMAL bar and press Enter"}
      </Text> : null}
    </Box>
  );
}
