// src/components/QuitPanel.tsx — truthful graceful-exit panel.
import React, { memo, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import { resolveQuitKey } from "../input/overlays/quit.js";
import { colors, useSelectionStyle } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  onQuit: () => void;
  onCancel: () => void;
};

const OPTIONS = [
  { label: "Quit", desc: "Close the Native Host and its managed daemons, then exit", action: "quit" as const },
  { label: "Cancel", desc: "Return to dashboard", action: "cancel" as const },
];

export const QuitPanel = memo(function QuitPanel({ onQuit, onCancel }: Props) {
  const [sel, setSel] = useState(0);

  useInput((input: string, key: any) => {
    for (const a of resolveQuitKey(input, key)) {
      if (a.type === "dismiss") {
        onCancel();
      } else if (a.type === "move") {
        setSel((s) => Math.max(0, Math.min(OPTIONS.length - 1, s + a.delta)));
      } else if (a.type === "confirm") {
        const chosen = OPTIONS[sel];
        switch (chosen?.action) {
          case "quit": onQuit(); break;
          case "cancel": onCancel(); break;
        }
      }
    }
  });

  return (
    <Box flexDirection="column" padding={1}>
      <Box marginBottom={1}>
        <Text bold>Quit gitgo</Text>
      </Box>

      {OPTIONS.map((opt, i) => {
        const active = i === sel;
        const optStyle = useSelectionStyle(active ? "focused" : "non-focused", "block", "success");
        return (
          <Box key={opt.action} flexDirection="column" marginBottom={1}>
            <Box>
              <Text
                color={optStyle.fg}
                backgroundColor={optStyle.bg}
                bold={optStyle.bold}
              >
                {opt.label}
              </Text>
            </Box>
            <Box paddingLeft={2}>
              <Text dimColor={!active}>{opt.desc}</Text>
            </Box>
          </Box>
        );
      })}

      <Box marginTop={1}>
        <Text dimColor>{chordLabel("upDown")} select  {chordLabel("enter")} confirm  {chordLabel("escape")} cancel</Text>
      </Box>
    </Box>
  );
});
