// src/components/Overview.tsx
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProjectRow } from "../hooks/useGitgoData.js";

type Props = { projects: ProjectRow[]; sel: number; focus: string; cols: number };

export const Overview = memo(function Overview({ projects, sel, focus, cols }: Props) {
  const w = Math.max(60, cols);
  const markerW = 2;
  const statusW = 3;
  const nameW = Math.max(10, Math.floor(w * 0.14));
  const procW = 4;
  const lessonsW = Math.max(8, Math.floor(w * 0.10));
  const contractW = Math.max(12, Math.floor(w * 0.15));
  const techW = Math.floor(w * 0.30);
  const pathW = Math.max(10, w - markerW - statusW - nameW - procW - lessonsW - contractW - techW - 2);

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} flexGrow={1}>
      <Box flexDirection="column" marginBottom={1}>
        <Text bold color="cyan">Gitgo Monitor</Text>
      </Box>
      <Box flexDirection="column" marginBottom={1}>
        <Text dimColor>↑↓ select  Enter detail  :cmd  h help  q quit</Text>
      </Box>

      {/* Header row — bold only, no underline */}
      <Box flexDirection="row" marginBottom={1}>
        <Box width={markerW}><Text> </Text></Box>
        <Box width={statusW}><Text bold dimColor>S</Text></Box>
        <Box width={nameW}><Text bold>Project</Text></Box>
        <Box width={procW}><Text bold dimColor>Proc</Text></Box>
        <Box width={lessonsW}><Text bold>Lessons</Text></Box>
        <Box width={contractW}><Text bold>Contract</Text></Box>
        <Box width={techW}><Text bold>Tech Stack</Text></Box>
        <Box width={pathW}><Text bold dimColor>Path</Text></Box>
      </Box>

      {projects.map((p, i) => {
        const isSelected = i === sel;
        const tableFocused = focus === "table";
        // Status indicator: ● green=online+active, ◐ yellow=online no proc, ○ gray=offline
        const statusIcon = p.daemonOnline
          ? (p.activeProcessCount > 0 ? "●" : "◐")
          : "○";
        const statusColor = p.daemonOnline
          ? (p.activeProcessCount > 0 ? "green" : "yellow")
          : "gray";
        const procBadge = p.activeProcessCount > 0 ? `[${p.activeProcessCount}]` : "";
        return (
          <Box key={p.name} flexDirection="row" marginBottom={1}>
            <Box width={markerW}>
              <Text color={isSelected && tableFocused ? "cyan" : undefined}>
                {isSelected && tableFocused ? "▶" : " "}
              </Text>
            </Box>
            <Box width={statusW}>
              <Text color={statusColor}>{statusIcon}</Text>
            </Box>
            <Box width={nameW}>
              <Text
                color={isSelected && tableFocused ? "cyan" : undefined}
                bold={isSelected}
              >
                {p.name.length > nameW ? p.name.slice(0, nameW - 1) + "…" : p.name}
              </Text>
            </Box>
            <Box width={procW}>
              <Text color="cyan" dimColor={!procBadge}>{procBadge || "-"}</Text>
            </Box>
            <Box width={lessonsW}>
              <Text>{String(p.pendingLessons)}</Text>
            </Box>
            <Box width={contractW}>
              <Text>{p.features}f/{p.constraints}c</Text>
            </Box>
            <Box width={techW}>
              <Text dimColor>
                {p.techStack.length > techW ? p.techStack.slice(0, techW - 1) + "…" : p.techStack}
              </Text>
            </Box>
            <Box width={pathW}>
              <Text dimColor>
                {(p.workspace.split("/").pop() || p.workspace.split("\\").pop() || "").slice(0, pathW)}
              </Text>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
});
