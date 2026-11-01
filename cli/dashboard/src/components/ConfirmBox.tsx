import {SelectionText} from "./Selection.js";
// src/components/ConfirmBox.tsx — shared Yes/No confirm sub-panel for destructive actions.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import { colors} from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";

type Props = {
  title: string;
  danger?: string;
  confirmSel: number;
};

export function ConfirmBox({ title, danger, confirmSel }: Props) {
  const t = useCopy();


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
        <SelectionText selected={confirmSel === 0} variant="block" accent="confirm-yes">{t({en:"Yes",zh:"确认"})}</SelectionText>
        <Text>     </Text>
        <SelectionText selected={confirmSel === 1} variant="block" accent="confirm-no">{t({en:"No",zh:"取消"})}</SelectionText>
      </Box>
      <Text dimColor>{t({en:"↑/↓ select · Enter confirm · Esc cancel",zh:"↑/↓ 选择 · Enter 确认 · Esc 取消",keys:true})}</Text>
    </Box>
  );
}
