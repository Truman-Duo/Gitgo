// src/components/ContractTab.tsx — Context panel: Contract summary
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import { colors } from "../theme/index.js";

type Props = { contract: any; width: number };

export const ContractTab = memo(function ContractTab({ contract, width }: Props) {
  if (!contract || contract.error) {
    return <Text dimColor>Loading...</Text>;
  }
  const features = contract.decided_features || [];
  const constraints = contract.architecture_constraints || [];
  const ts = contract.tech_stack?.join(", ") || "-";
  return (
    <Box flexDirection="column">
      <Box>
        <Text bold>Project Contract</Text>
        <Text dimColor> (contract.yaml)</Text>
      </Box>
      <Text bold>Tech Stack</Text>
      <Text dimColor>{ts}</Text>
      <Box marginTop={1}>
        <Text bold>Features ({features.length})</Text>
        {features.slice(0, 10).map((f: any, i: number) => (
          <Text key={i} dimColor>{"  "}{f.name || f}</Text>
        ))}
      </Box>
      <Box marginTop={1}>
        <Text bold color={colors.danger}>Constraints ({constraints.length})</Text>
        {constraints.slice(0, 10).map((c: any, i: number) => (
          <Text key={i} dimColor>{"  "}{c.name || c}</Text>
        ))}
      </Box>
    </Box>
  );
});
