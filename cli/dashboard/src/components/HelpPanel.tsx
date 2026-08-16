// src/components/HelpPanel.tsx — v5: scene-specific keyboard reference with useInput dismiss
import React, { memo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { Scene } from "../state/store.js";
import { resolveHelpKey } from "../input/overlayKeymaps.js";
import { colors } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = { scene: Scene; onDismiss: () => void };

function commandsForScene(scene: Scene): { label: string; desc: string }[] {
  switch (scene) {
    case "projects":
      return [
        { label: "/create <name>", desc: "Create new project" },
        { label: "/archive [name]", desc: "Archive manager" },
        { label: "/status", desc: "Global project status" },
        { label: "/export [project]", desc: "Export project knowledge" },
        { label: "/config", desc: "Project settings (LLM / publish / safety)" },
        { label: "/quit", desc: "Save and exit safely" },
      ];
    case "workspace":
      return [
        { label: "/runtime", desc: "Runtime data (lesson/contract/governance/memory...)" },
        { label: "/quit", desc: "Save and exit safely" },
      ];
    case "agent_detail":
      return [
        { label: "/runtime", desc: "Agent runtime data (status/context/lesson...)" },
        { label: "/quit", desc: "Save and exit safely" },
      ];
    default:
      return [
        { label: "/quit", desc: "Save and exit safely" },
      ];
  }
}

export const HelpPanel = memo(function HelpPanel({ scene, onDismiss }: Props) {
  useInput((input: string, key: any) => {
    for (const a of resolveHelpKey(input, key)) {
      if (a.type === "dismiss") onDismiss();
    }
  });

  const cmds = commandsForScene(scene);

  return (
    <Box flexDirection="column" padding={1}>
      <Box>
        <Text bold>Keyboard Reference</Text>
        <Text dimColor>    {chordLabel("escape")} or {chordLabel("letterH")} to dismiss</Text>
      </Box>

      <Box flexDirection="row" gap={2}>
        <Box flexDirection="column" marginRight={2}>
          <Box>
            <Text bold color={colors.accent}>Navigation</Text>
          </Box>
          <Text color={colors.accent}>{chordLabel("upDown")}  Navigate</Text>
          <Text color={colors.accent}>{chordLabel("enter")}  Select / Send</Text>
          <Text color={colors.accent}>{chordLabel("left")}      Back</Text>
          <Text color={colors.accent}>{chordLabel("escape")}    Interrupt / Cancel</Text>
          <Text color={colors.accent}>{chordLabel("slash")}      Command mode</Text>
          <Text color={colors.accent}>{chordLabel("tab")}    Next panel</Text>
        </Box>

        <Box flexDirection="column">
          <Box>
            <Text bold color={colors.warning}>Commands</Text>
          </Box>
          {cmds.map((c) => (
            <Text key={c.label} dimColor>{c.label.padEnd(18)} {c.desc}</Text>
          ))}
        </Box>
      </Box>
    </Box>
  );
});
