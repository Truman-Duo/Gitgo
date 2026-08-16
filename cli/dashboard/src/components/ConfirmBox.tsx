// src/components/ConfirmBox.tsx — shared Yes/No confirm sub-panel for destructive actions.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import { colors, useSelectionStyle } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  title: string;
  danger?: string;
  confirmSel: number;
};

export function ConfirmBox({ title, danger, confirmSel }: Props) {
  const yesStyle = useSelectionStyle(confirmSel === 0 ? "focused" : "non-focused", "block", "confirm-yes");
  const noStyle = useSelectionStyle(confirmSel === 1 ? "focused" : "non-focused", "block", "confirm-no");
  return (
    <Box flexDirection="column" padding={1}>
      <Box marginBottom={1}>
        <Text bold>{title}</Text>
      </Box>
      {danger ? (
        <Box marginBottom={1}>
          <Text color={colors.danger}>{danger}</Text>
        </Box>
      ) : null}
      <Box flexDirection="row" marginBottom={1}>
        <Text color={yesStyle.fg} backgroundColor={yesStyle.bg} bold={yesStyle.bold}>Yes</Text>
        <Text>     </Text>
        <Text color={noStyle.fg} backgroundColor={noStyle.bg} bold={noStyle.bold}>No</Text>
      </Box>
      <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} confirm    {chordLabel("escape")} cancel</Text>
    </Box>
  );
}
