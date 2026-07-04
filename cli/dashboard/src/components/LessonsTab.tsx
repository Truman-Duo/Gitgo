// src/components/LessonsTab.tsx — Context panel: Pending lessons
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";

type Props = { lessons: any; width: number };

export const LessonsTab = memo(function LessonsTab({ lessons, width }: Props) {
  if (!lessons) return <Text dimColor>加载中...</Text>;
  const pending = lessons?.pending || [];
  if (pending.length === 0) return <Text dimColor>无待确认 lesson</Text>;
  return (
    <Box flexDirection="column">
      <Text bold>Pending ({pending.length})</Text>
      {pending.slice(0, 15).map((l: any, i: number) => {
        const sevColor =
          l.severity === "critical" || l.severity === "high" ? "red"
          : l.severity === "medium" ? "yellow"
          : "gray";
        return (
          <Box key={i} flexDirection="column" marginBottom={1}>
            <Text>
              <Text color={sevColor}>
                [{l.severity?.[0]?.toUpperCase() || "?"}]
              </Text>
              {" "}{l.trigger?.slice(0, 60) || l.id?.slice(0, 12)}
            </Text>
          </Box>
        );
      })}
    </Box>
  );
});
