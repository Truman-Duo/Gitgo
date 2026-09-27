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
  const options = ["Rewind conversation", "Keep current conversation"];
  return <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
    <Text bold>Session rewind</Text>
    <Text>Remove the latest user turn and its following conversation from active context?</Text>
    {preview.turn_preview ? <Text dimColor>Turn: {String(preview.turn_preview)}</Text> : null}
    <Text dimColor>{String(preview.warning || "Workspace and external effects are retained.")}</Text>
    <Box flexDirection="row" marginTop={1}>
      {options.map((label, index) => <React.Fragment key={label}>
        {index > 0 ? <Text>  </Text> : null}
        <Text
          color={index === selection ? colors.selection.block.fg : undefined}
          backgroundColor={index === selection ? colors.selection.block.bg : undefined}
          bold={index === selection}
        > {label} </Text>
      </React.Fragment>)}
    </Box>
    {busy ? <Text dimColor>Rewinding…</Text> : null}
    {error ? <Text color={colors.danger}>[SESSION_UNDO_FAILED] {error}</Text> : null}
    <Text dimColor>←/→ choose · Enter confirm · Esc cancel</Text>
  </Box>;
}
