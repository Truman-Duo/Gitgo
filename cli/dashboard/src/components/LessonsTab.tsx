// src/components/LessonsTab.tsx — v4 blueprint: Pending + Abstract + Instances layers
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import { colors } from "../theme/index.js";

type Props = { lessons: any; width: number };

const SEV_LABEL: Record<string, string> = {
  critical: "C", high: "H", medium: "M", low: "L",
};

const SEV_COLOR: Record<string, string> = {
  critical: colors.named.red,
  high: colors.named.yellow,
  medium: colors.named.gray,
  low: colors.named.gray,
};

export const LessonsTab = memo(function LessonsTab({ lessons, width }: Props) {
  if (!lessons) return <Text dimColor>Loading...</Text>;

  const pending = lessons?.pending || [];
  const abstract = lessons?.abstract || [];
  const instances = lessons?.instances || [];
  const hasAny = pending.length > 0 || abstract.length > 0 || instances.length > 0;
  if (!hasAny) return <Text dimColor>No lessons recorded</Text>;

  return (
    <Box flexDirection="column">
      {/* Pending */}
      {pending.length > 0 ? (
        <Box flexDirection="column">
          <Text bold>Pending ({pending.length})</Text>
          {pending.slice(0, 10).map((l: any, i: number) => {
            const sev = l.severity || "medium";
            const letter = SEV_LABEL[sev] || "M";
            const color = SEV_COLOR[sev] || "gray";
            const trigger = l.trigger || l.description || "?";
            return (
              <Box key={i} flexDirection="row">
                <Text color={color}>[{letter}]</Text>
                <Text dimColor> {trigger.slice(0, width - 10)}</Text>
              </Box>
            );
          })}
        </Box>
      ) : null}

      {/* Abstract */}
      {abstract.length > 0 ? (
        <Box flexDirection="column">
          <Text bold>Abstract ({abstract.length})</Text>
          {abstract.slice(0, 10).map((a: any, i: number) => {
            const rule = a.rule || a.id || "?";
            const sev = a.severity || "medium";
            const letter = SEV_LABEL[sev] || "M";
            const color = SEV_COLOR[sev] || "gray";
            return (
              <Box key={i} flexDirection="row">
                <Text color={color}>[{letter}]</Text>
                <Text dimColor> {rule.slice(0, width - 10)}</Text>
              </Box>
            );
          })}
        </Box>
      ) : null}

      {/* Instances */}
      {instances.length > 0 ? (
        <Box flexDirection="column">
          <Text bold>Instances ({instances.length})</Text>
          {instances.slice(0, 10).map((ins: any, i: number) => {
            const loc = ins.file
              ? `${ins.file}${ins.line ? `:${ins.line}` : ""}`
              : "?";
            const date = ins.applied_at || ins.verified_at || "";
            const dateStr = date ? date.slice(0, 10) : "";
            const sev = ins.severity || "medium";
            const letter = SEV_LABEL[sev] || "L";
            const color = SEV_COLOR[sev] || "gray";
            return (
              <Box key={i} flexDirection="row">
                <Text color={color}>[{letter}]</Text>
                <Text dimColor> {loc.slice(0, width - 20)}</Text>
                {dateStr ? <Text dimColor> {"—"} {dateStr}</Text> : null}
              </Box>
            );
          })}
        </Box>
      ) : null}
    </Box>
  );
});
