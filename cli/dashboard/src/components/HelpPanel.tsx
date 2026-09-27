import React, { memo } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { Scene } from "../state/store.js";
import { resolveHelpKey } from "../input/overlays/help.js";
import { getKeybindings } from "../keybindings.js";
import { chordLabel } from "../input/bindings.js";

type Props = { scene: Scene; onDismiss: () => void };

/** Help projects the command/key registries instead of copying another menu. */
export const HelpPanel = memo(function HelpPanel({ scene, onDismiss }: Props) {
  useInput((input: string, key: any) => {
    for (const action of resolveHelpKey(input, key)) {
      if (action.type === "dismiss") onDismiss();
    }
  });
  const commands = getKeybindings(scene);
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
    <Text bold>Help</Text>
    <Text dimColor>
      {chordLabel("upDown")} navigate · {chordLabel("enter")} select/send · {chordLabel("left")} back · {chordLabel("escape")} cancel · {chordLabel("slash")} commands
    </Text>
    {(scene === "workspace" || scene === "agent_detail")
      ? <Text dimColor>{chordLabel("ctrlG")} edit the complete prompt in an external editor</Text>
      : null}
    <Box flexDirection="column" marginTop={1}>
      {commands.map(command => <Text key={command.name}>
        {(`/${command.slashName}`).padEnd(18)} <Text dimColor>{command.title}</Text>
      </Text>)}
    </Box>
    <Text dimColor>{chordLabel("escape")} close</Text>
  </Box>;
});
