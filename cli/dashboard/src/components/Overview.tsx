// src/components/Overview.tsx — 5-column blueprint-aligned project list
// Blueprint grid: Status(10) | Project(1fr) | Procs(7) | Lessons(8) | Path(1fr)
// Uses string padding for column alignment — no inner Box wrappers so
// backgroundColor on Text elements drives correct Ink dirty/blit behavior.
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import { colors, usePanelSize, statusDot, truncate, useSelectionStyle, projectStatusDot, partitionByRank } from "../theme/index.js";
import type { StatusState } from "../theme/index.js";

type Props = { projects: ProjectRow[]; sel: number; mode: "NORMAL" | "COMMAND"; cols: number; listActive: boolean };

export const Overview = memo(function Overview({ projects, sel, mode, cols: _cols, listActive }: Props) {
  const { w } = usePanelSize({ minWidth: 60 });
  const statusW = 3;
  const procW = 7;
  const lessonsW = 8;
  const fixedW = statusW + procW + lessonsW;
  const flexW = w - fixedW;
  const nameW = Math.floor(flexW * 0.5);
  const pathW = flexW - nameW;
  const tableFocused = listActive;

  // Projects arrive pre-ordered (running → pending → finished, each alphabetical).
  const { running, pending, finished } = partitionByRank(
    projects,
    (p) =>
      p.daemonOnline && p.activeProcessCount > 0 ? 0
      : p.daemonOnline && p.waitingProcessCount > 0 ? 1 : 2,
  );
  const groups = [
    { label: "Running", items: running, start: 0 },
    { label: "Pending", items: pending, start: running.length },
    { label: "Finished", items: finished, start: running.length + pending.length },
  ];

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} flexGrow={1}>
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor>Projects</Text>
      </Box>

      {/* Header row — 5 columns padded to width */}
      <Box flexDirection="row">
        <Text bold dimColor>{"●".padEnd(statusW)}</Text>
        <Text bold>{"Name".padEnd(nameW)}</Text>
        <Text bold dimColor>{"Procs".padEnd(procW)}</Text>
        <Text bold dimColor>{"Lessons".padEnd(lessonsW)}</Text>
        <Text bold dimColor>Path</Text>
      </Box>

      {groups.map((group) => {
        if (group.items.length === 0) return null;
        return (
          <React.Fragment key={group.label}>
            <Text dimColor bold>
              {group.label} ({group.items.length})
            </Text>
            {group.items.map((p, gi) => {
              const i = group.start + gi;
              const isSelected = i === sel;
              const highlightBg = isSelected && tableFocused ? colors.selection.row.bg : undefined;
              const nameStyle = useSelectionStyle(isSelected && tableFocused ? "focused" : "non-focused", "row");

              const dot = statusDot(projectStatusDot(p.daemonOnline, p.activeProcessCount));
              const statusStr = dot.char.padEnd(statusW);

              const procBadge = p.activeProcessCount > 0 ? String(p.activeProcessCount) : "";
              const procColor = p.activeProcessCount > 0 ? colors.accent : undefined;
              const procStr = (procBadge || "-").padEnd(procW);

              const lessonNum = p.pendingLessons;
              const lessonColor = lessonNum > 0 ? colors.warning : undefined;
              const lessonsStr = String(lessonNum).padEnd(lessonsW);

              const pathBase = p.workspace.split("/").pop() || p.workspace.split("\\").pop() || p.workspace;
              const paddedName = truncate(p.name, nameW).padEnd(nameW);
              const pathStr = truncate(pathBase, pathW);

              return (
                <Box key={p.name} flexDirection="row" backgroundColor={highlightBg}>
                  <Text color={dot.color} backgroundColor={highlightBg}>{statusStr}</Text>
                  <Text
                    color={nameStyle.fg}
                    bold={nameStyle.bold}
                    backgroundColor={highlightBg}
                  >
                    {paddedName}
                  </Text>
                  <Text color={procColor} dimColor={!procBadge} backgroundColor={highlightBg}>{procStr}</Text>
                  <Text color={lessonColor} dimColor={!lessonNum} backgroundColor={highlightBg}>{lessonsStr}</Text>
                  <Text dimColor backgroundColor={highlightBg}>{pathStr}</Text>
                </Box>
              );
            })}
          </React.Fragment>
        );
      })}
    </Box>
  );
});
