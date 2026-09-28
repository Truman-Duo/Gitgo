// src/components/Overview.tsx — compact project list
// Uses string padding for column alignment — no inner Box wrappers so
// backgroundColor on Text elements drives correct Ink dirty/blit behavior.
import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import { colors, usePanelSize, statusDot, truncate, padEndWidth, useSelectionStyle, projectStatusDot } from "../theme/index.js";
import type { StatusState } from "../theme/index.js";
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
  const finished = projects.filter(p => projectRuntimeCategory(p) === "finished");
  const newProjects = projects.filter(p => projectRuntimeCategory(p) === "new");
  const groups = [
    { label: language === "zh" ? "运行中" : "Running", items: running, start: 0 },
    { label: language === "zh" ? "等待中" : "Pending", items: pending, start: running.length },
    { label: language === "zh" ? "不可用" : "Unavailable", items: unavailable, start: running.length + pending.length },
    { label: language === "zh" ? "已完成" : "Finished", items: finished, start: running.length + pending.length + unavailable.length },
    { label: language === "zh" ? "新建" : "New", items: newProjects, start: running.length + pending.length + unavailable.length + finished.length },
  ];

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} paddingTop={1} flexGrow={1}>
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor>{language === "zh" ? "项目" : "Projects"}</Text>
      </Box>

      {/* Status is a row-level signal inside Name, not a separate column. */}
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

              const dot = statusDot(projectStatusDot(p.daemonOnline, p.activeProcessCount));
              const procBadge = p.activeProcessCount > 0 ? String(p.activeProcessCount) : "";
              const procColor = p.activeProcessCount > 0 ? colors.accent : undefined;
              const procStr = (procBadge || "-").padEnd(procW);

              const pathBase = p.workspace.split("/").pop() || p.workspace.split("\\").pop() || p.workspace;
              const nameTextW = Math.max(1, nameW - 2);
              const paddedName = padEndWidth(truncate(p.name, nameTextW), nameTextW);
              const pathStr = truncate(pathBase, pathW);

              return (
                <Box key={p.name} flexDirection="row" backgroundColor={highlightBg}>
                  <Text color={dot.color} backgroundColor={highlightBg}>{dot.char} </Text>
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
