import {SelectionText} from "./Selection.js";
import {useCopy} from "../ui/PageFrame.js";
import React, { useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { commitUndo } from "../backend/tools.js";
import { colors } from "../theme/index.js";

export function UndoPanel({ client, project, processId, preview, onDismiss, onCommitted }: {
  client: BackendClient; project: string; processId: string; preview: any;
  onDismiss: () => void; onCommitted: () => void;
}) {
  const t=useCopy();
  const [selection, setSelection] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useInput((_input, key) => {
    if (busy) return;
    if (key.escape) { onDismiss(); return; }
    if (key.leftArrow || key.rightArrow || key.upArrow || key.downArrow) {
      setSelection(value => value === 0 ? 1 : 0);
      return;
    }
    if (key.return) {
      if (selection === 1) { onDismiss(); return; }
      setBusy(true);
      setError("");
      void commitUndo(client, project, processId, String(preview.checkpoint_id || ""))
        .then(() => { onDismiss(); onCommitted(); })
        .catch(err => setError(String(err)))
        .finally(() => setBusy(false));
    }
  });
  const options = [t({en:"Rewind conversation",zh:"撤回对话"}), t({en:"Keep current conversation",zh:"保留当前对话"})];
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
    <Text bold>{t({en:"Session rewind",zh:"会话撤回"})}</Text>
    <Text>{t({en:"Remove the latest user turn and its following conversation from active context?",zh:"从活动上下文移除最近一轮用户输入及随后对话？"})}</Text>
    {preview.turn_preview ? <Text dimColor>{t({en:"Turn: ",zh:"轮次： "})}{String(preview.turn_preview)}</Text> : null}
    <Text dimColor>{String(preview.warning || t({en:"Workspace and external effects are retained.",zh:"保留工作区和已发生的外部效果。"}))}</Text>
    <Box flexDirection="row" marginTop={1}>
      {options.map((label, index) => <React.Fragment key={label}>
        {index > 0 ? <Text>  </Text> : null}
        <SelectionText selected={index === selection} variant="block"



        > {label} </SelectionText>
      </React.Fragment>)}
    </Box>
    {busy ? <Text dimColor>{t({en:"Rewinding…",zh:"正在撤回…"})}</Text> : null}
    {error ? <Text color={colors.danger}>{t({en:"[SESSION_UNDO_FAILED] ",zh:"[SESSION_UNDO_FAILED] "})}{error}</Text> : null}
    <Text dimColor>{t({en:"←/→ choose · Enter confirm · Esc cancel",zh:"←/→ 选择 · Enter 确认 · Esc 取消",keys:true})}</Text>
  </Box>;
}
