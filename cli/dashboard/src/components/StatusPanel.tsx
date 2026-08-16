// src/components/StatusPanel.tsx — /status: global project health overview
// Shows all active projects with key metrics. ↑↓ to scroll, Enter to enter project.
import React, { memo, useState } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { ProjectRow } from "../hooks/useGitgoData.js";
import { resolveStatusKey } from "../input/overlays/status.js";
import { colors, statusDot, truncate, useSelectionStyle } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  projects: ProjectRow[];
  cols: number;
  onDismiss: () => void;
  onEnterProject: (name: string) => void;
};

export const StatusPanel = memo(function StatusPanel({
  projects, cols, onDismiss, onEnterProject,
}: Props) {
  const [sel, setSel] = useState(0);
  const active = projects.filter((p) => !(p as any).archived);
  const totalLessons = active.reduce((s, p) => s + p.pendingLessons, 0);
  const online = active.filter((p) => p.daemonOnline).length;

  useInput((input: string, key: any) => {
    for (const a of resolveStatusKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setSel((s) => Math.max(0, Math.min(active.length - 1, s + a.delta)));
      } else if (a.type === "confirm") {
        if (active[sel]) {
          onEnterProject(active[sel].name);
          onDismiss();
        }
      }
    }
  });

  return (
    <Box flexDirection="column" padding={1} width={cols}>
      <Box marginBottom={1}>
        <Text bold>Status</Text>
        <Text dimColor>
          {"    "}{active.length} projects    {online} daemons online    {totalLessons} pending lessons
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Box flexDirection="row">
          <Text dimColor>{"Project".padEnd(20)}</Text>
          <Text dimColor>{"Daemon".padEnd(8)}</Text>
          <Text dimColor>{"Lessons".padEnd(10)}</Text>
          <Text dimColor>{"Status"}</Text>
        </Box>
      </Box>

      {active.map((p, i) => {
        const isSel = i === sel;
        const selStyle = useSelectionStyle(isSel ? "focused" : "non-focused", "row");
        const daemonDot = statusDot(p.daemonOnline ? "ok" : "offline");
        return (
          <Box key={p.name} marginBottom={1}>
            <Box flexDirection="row">
              <Text
                color={selStyle.fg}
                backgroundColor={selStyle.bg}
                bold={selStyle.bold}
              >
                {truncate(p.name, 18).padEnd(20)}
              </Text>
              <Text
                color={daemonDot.color ?? selStyle.fg}
                backgroundColor={selStyle.bg}
              >
                {"  "}{daemonDot.char}{p.daemonOnline ? " online" : " offline"}
                {"  ".repeat(2)}
              </Text>
              <Text
                color={selStyle.fg}
                backgroundColor={selStyle.bg}
              >
                {String(p.pendingLessons).padEnd(10)}
              </Text>
              <Text
                color={selStyle.fg}
                backgroundColor={selStyle.bg}
              >
                {p.governanceStatus || "?"}
                {p.activeProcessCount > 0 ? `  ${p.activeProcessCount} agents` : ""}
              </Text>
            </Box>
            {isSel && (
              <Box paddingLeft={2}>
                <Text dimColor>
                  workspace: {p.workspace || "?"}{"\n"}
                  contract: {p.features}f/{p.constraints}c  {p.techStack || "?"}{"\n"}
                  LLM: {p.llmProviderSummary || "none"}
                </Text>
              </Box>
            )}
          </Box>
        );
      })}

      <Box marginTop={1}>
        <Text dimColor>
          {active.length === 0
            ? "No active projects. Use /create to add one."
            : `${chordLabel("upDown")} select    ${chordLabel("enter")}  open project    ${chordLabel("escape")} back`}
        </Text>
      </Box>
    </Box>
  );
});
