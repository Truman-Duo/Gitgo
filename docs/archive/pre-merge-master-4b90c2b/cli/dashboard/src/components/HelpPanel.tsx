// src/components/HelpPanel.tsx
import React, { memo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";

type Props = { onDismiss: () => void };

export const HelpPanel = memo(function HelpPanel({ onDismiss }: Props) {
  useInput((input: string, key: any) => {
    if (key.escape || input === "h") {
      onDismiss();
    }
  });

  return (
    <Box
      flexDirection="column"
      padding={1}
      borderStyle="single"
      borderColor="blue"
    >
      <Text bold>Keyboard:</Text>
      <Text>  ↑↓     Select project / item</Text>
      <Text>  Enter  View detail / drill into item</Text>
      <Text>  ←→     Detail tab switch</Text>
      <Text>  Esc    Back (item→list→overview)</Text>
      <Text>  :      Enter command mode</Text>
      <Text>  h      Toggle help</Text>
      <Text>  q      Quit (from overview)</Text>
      <Text> </Text>
      <Text bold>Commands:</Text>
      <Text>  l[esson]  [proj]   Lesson summary + IDs</Text>
      <Text>  c[ontract] [proj]   Contract summary</Text>
      <Text>  s[tatus]  [proj]   Status + next action</Text>
      <Text>  v[erify]  {"<id>"}    Verify a lesson</Text>
      <Text>  p[roject] {"<name>"}  Jump to project</Text>
      <Text>  r[efresh]           Force refresh</Text>
      <Text>  h[elp]              This panel</Text>
      <Text> </Text>
      <Text dimColor>Press h or Esc to dismiss</Text>
    </Box>
  );
});
