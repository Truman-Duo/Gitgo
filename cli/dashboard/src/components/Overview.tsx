// src/components/Overview.tsx — compact project list
// Uses string padding for column alignment — no inner Box wrappers so
// backgroundColor on Text elements drives correct Ink dirty/blit behavior.
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import { colors, usePanelSize, truncate, padEndWidth, useSelectionStyle } from "../theme/index.js";
import { projectRuntimeCategory } from "../projectRuntimeState.js";

type Props = { projects: ProjectRow[]; sel: number; mode: "NORMAL" | "COMMAND"; cols: number; listActive: boolean; language?: "en" | "zh" };

export const Overview = memo(function Overview({ projects, sel, mode, cols: _cols, listActive, language = "en" }: Props) {
  const { w } = usePanelSize({ minWidth: 60 });
  const procW = 7;
  const fixedW = procW;
  const flexW = w - fixedW;
  const nameW = Math.floor(flexW * 0.5);
  const pathW = flexW - nameW;
  const tableFocused = listActive;

  // Projects arrive pre-ordered by the same canonical state projection.
  const running = projects.filter(p => projectRuntimeCategory(p) === "running");
  const pending = projects.filter(p => projectRuntimeCategory(p) === "pending");
  const unavailable = projects.filter(p => projectRuntimeCategory(p) === "unavailable");
  const groups = [
    { label: language === "zh" ? "运行中" : "Running", items: running, start: 0 },
    { label: language === "zh" ? "已完成 / 等待" : "Completed / Waiting", items: pending, start: running.length },
    { label: language === "zh" ? "不可用" : "Unavailable", items: unavailable, start: running.length + pending.length },
  ];

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} flexGrow={1}>
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor>{language === "zh" ? "项目" : "Projects"}</Text>
      </Box>

      <Box flexDirection="row">
        <Text bold>{"Name".padEnd(nameW)}</Text>
        <Text bold dimColor>{"Procs".padEnd(procW)}</Text>
        <Text bold dimColor>Path</Text>
      </Box>

      {projects.length === 0 ? (
        <Box flexDirection="column" paddingTop={1}>
          <Text dimColor>{language === "zh" ? "还没有项目。" : "No projects yet."}</Text>
          <Text dimColor>{language === "zh" ? "使用 /create 注册工作区，然后按 Enter 打开。" : "Use /create to register a workspace, then Enter to open it."}</Text>
        </Box>
      ) : null}

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

              const procBadge = p.activeProcessCount > 0 ? String(p.activeProcessCount) : "";
              const procColor = p.activeProcessCount > 0 ? colors.accent : undefined;
              const procStr = (procBadge || "-").padEnd(procW);

              const pathBase = p.workspace.split(/[\\/]/).filter(Boolean).pop() || p.workspace;
              const nameTextW = Math.max(1, nameW);
              const paddedName = padEndWidth(truncate(p.name, nameTextW), nameTextW);
              const pathStr = truncate(pathBase, pathW);

              return (
                <Box key={p.name} flexDirection="row" backgroundColor={highlightBg}>
                  <Text
                    color={nameStyle.fg}
                    bold={nameStyle.bold}
                    backgroundColor={highlightBg}
                  >
                    {paddedName}
                  </Text>
                  <Text color={procColor} dimColor={!procBadge} backgroundColor={highlightBg}>{procStr}</Text>
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
