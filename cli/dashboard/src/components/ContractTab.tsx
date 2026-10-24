import {useCopy} from "../ui/PageFrame.js";
// src/components/ContractTab.tsx — Context panel: Contract summary
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import { colors } from "../theme/index.js";

type Props = { contract: any; width: number };

export const ContractTab = memo(function ContractTab({ contract, width }: Props) {
  const t=useCopy();
  if (!contract) {
    return <Text dimColor>{t({en:"Loading...",zh:"正在读取…"})}</Text>;
  }
  if (contract.error) return <Text dimColor>{String(contract.error)}</Text>;
  if (Object.prototype.hasOwnProperty.call(contract,"contract") && contract.contract === null) {
    return <Text dimColor>{t({en:"No project contract has been created yet.",zh:"尚未建立项目契约。"})}</Text>;
  }
  const value=contract.contract||contract;
  const features = value.decided_features || [];
  const constraints = value.architecture_constraints || [];
  const ts = value.tech_stack?.join(", ") || "-";
  return (
    <Box flexDirection="column">
      <Box>
        <Text bold>{t({en:"Project Contract",zh:"项目契约"})}</Text>
        <Text dimColor> (contract.yaml)</Text>
      </Box>
      <Text bold>{t({en:"Tech Stack",zh:"技术栈"})}</Text>
      <Text dimColor>{ts}</Text>
      <Box marginTop={1}>
        <Text bold>{t({en:"Features",zh:"功能"})} ({features.length})</Text>
        {features.slice(0, 10).map((f: any, i: number) => (
          <Text key={i} dimColor>{"  "}{f.name || f}</Text>
        ))}
      </Box>
      <Box marginTop={1}>
        <Text bold color={colors.danger}>{t({en:"Constraints",zh:"约束"})} ({constraints.length})</Text>
        {constraints.slice(0, 10).map((c: any, i: number) => (
          <Text key={i} dimColor>{"  "}{c.name || c}</Text>
        ))}
      </Box>
    </Box>
  );
});
