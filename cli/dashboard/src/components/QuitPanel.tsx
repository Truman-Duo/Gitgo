import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {SelectionText} from "./Selection.js";
// src/components/QuitPanel.tsx — truthful graceful-exit panel.
import React, { memo, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import { resolveQuitKey } from "../input/overlays/quit.js";
import { colors} from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";

type Props = {
  onQuit: () => void;
  onCancel: () => void;
};

const OPTIONS = [
  { label: {en:"Quit",zh:"退出"}, desc: {en:"Close the Native Host and its managed daemons, then exit",zh:"关闭当前宿主和受管进程，然后退出"}, action: "quit" as const },
  { label: {en:"Cancel",zh:"取消"}, desc: {en:"Return to dashboard",zh:"返回看板"}, action: "cancel" as const },
];

export const QuitPanel = memo(function QuitPanel({ onQuit, onCancel }: Props) {
  const t=useCopy();
  const [sel, setSel] = useState(0);

  const menuHelp=useLocalHelpCommand("quit",undefined,true);
  useInput((input: string, key: any) => {
    if(menuHelp.handle(input,key))return;
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
        <Text bold>{t({en:"Quit Gitgo",zh:"退出 Gitgo"})}</Text>
      </Box>

      {OPTIONS.map((opt, i) => {
        const active = i === sel;

        return (
          <Box key={opt.action} flexDirection="column" marginBottom={1}>
            <Box>
              <SelectionText selected={active} variant="block" accent={"success"}



              >
                {t(opt.label)}
              </SelectionText>
            </Box>
            <Box paddingLeft={2}>
              <Text dimColor={!active}>{t(opt.desc)}</Text>
            </Box>
          </Box>
        );
      })}

      <Box marginTop={1}>

      </Box>
    </Box>
  );
});
