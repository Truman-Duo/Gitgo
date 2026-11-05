import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import {PageActions} from "../ui/PageActions.js";
import {useCopy} from "../ui/PageFrame.js";
import React, { useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { commitUndo } from "../backend/tools.js";
import { colors } from "../theme/index.js";
import {matchChord} from "../input/bindings.js";

export function UndoPanel({ client, project, processId, preview, onDismiss, onCommitted }: {
  client: BackendClient; project: string; processId: string; preview: any;
  onDismiss: () => void; onCommitted: () => void;
}) {
  const t=useCopy();
  const [selection, setSelection] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const menuHelp=useLocalHelpCommand("undo",undefined,!busy);
  useInput((_input, key) => {
    if(menuHelp.handle(_input,key))return;
    if (busy) return;
    if (matchChord("escape",_input,key)) { onDismiss(); return; }
    if ((["left","right","up","down"] as const).some(chord=>matchChord(chord,_input,key))) {
      setSelection(value => value === 0 ? 1 : 0);
      return;
    }
    if (matchChord("enter",_input,key)) {
      if (selection === 1) { onDismiss(); return; }
      setBusy(true);
      setError("");
      void commitUndo(client, project, processId, String(preview.checkpoint_id || ""))
        .then(() => { onDismiss(); onCommitted(); })
        .catch(err => setError(String(err)))
        .finally(() => setBusy(false));
    }
  },{clipboard:false});
  const options = [{id:0,enabled:true,label:{en:"Rewind conversation",zh:"撤回对话"}},{id:1,enabled:true,label:{en:"Keep current conversation",zh:"保留当前对话"}}];
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
    <Text bold>{t({en:"Session rewind",zh:"会话撤回"})}</Text>
    <Text>{t({en:"Remove the latest user turn and its following conversation from active context?",zh:"从活动上下文移除最近一轮用户输入及随后对话？"})}</Text>
    <Box marginTop={1}><PageActions items={options} selected={selection} focused/></Box>
    {busy ? <Text dimColor>{t({en:"Rewinding…",zh:"正在撤回…"})}</Text> : null}
    {error ? <Text color={colors.danger}>{t({en:"[SESSION_UNDO_FAILED] ",zh:"[SESSION_UNDO_FAILED] "})}{error}</Text> : null}

  </Box>;
}
