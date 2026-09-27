import React, { useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import { cancelRequest, stopProcess } from "../backend/tools.js";
import { useManagedInput } from "../input/runtime.js";
import { colors } from "../theme/index.js";

export const interruptionAccepted = (result: any): boolean => Boolean(
  result?.cancelled || result?.requested || result?.tree_terminal
  || result?.pending_admission || result?.already_cancelling?.length,
);

export async function requestInterruption(
  client: BackendClient, project: string, processId: string, requestId?: string,
): Promise<any> {
  let result: any = requestId
    ? await cancelRequest(client, requestId)
    : await stopProcess(client, project, processId);
  // Request correlation can disappear before the process itself reaches a
  // terminal state (for example after a stream-recovery boundary).  Once
  // admission has acknowledged a concrete process, that process is the
  // durable cancellation identity.  Fall back to it instead of telling the
  // user an interruption failed while the task visibly keeps running.
  if (!interruptionAccepted(result) && processId) {
    result = await stopProcess(client, project, processId);
  }
  return result;
}

export function InterruptPanel({ client, project, processId, requestId, subtree, onDismiss }: {
  client: BackendClient; project: string; processId: string; requestId?: string;
  subtree: boolean; onDismiss: () => void;
}) {
  const [selection, setSelection] = useState(1);
  const [status, setStatus] = useState("");
  const inFlight = useRef(false);
  const confirm = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setStatus("Interrupting… waiting for the host acknowledgement");
    try {
      // A request cancellation is stronger than a process-only stop: it also
      // covers recovery/admission before the process exists and asks the Host
      // to stop the whole correlated task tree once admission completes.
      const result = await requestInterruption(
        client, project, processId, requestId,
      );
      if (!interruptionAccepted(result)) {
        throw new Error(result?.status || "Host did not accept the interruption");
      }
      setStatus(result.tree_terminal ? "Execution stopped. Conversation and committed changes preserved."
        : "Interruption accepted; execution is stopping. Conversation and committed changes are preserved.");
    } catch (error) { setStatus(`[INTERRUPT_FAILED] ${String(error)}`); }
    finally { inFlight.current = false; }
  };
  useManagedInput((_input, key) => {
    if (key.escape) { onDismiss(); return; }
    if (inFlight.current) return;
    if (key.leftArrow || key.rightArrow) setSelection(value => 1 - value);
    if (key.return) { if (selection === 1) onDismiss(); else void confirm(); }
  });
  return <Box flexDirection="column" paddingX={1}>
    <Text bold>Interrupt {subtree ? "the main process and its active subprocesses" : "this subprocess"}?</Text>
    <Text>Stop the current calls; preserve the conversation and completed file changes. No automatic rollback.</Text>
    <Box gap={2}>{["Interrupt", "Keep working"].map((label, index) => <Text key={label}
      backgroundColor={selection === index ? colors.selection.block.bg : undefined}
      color={selection === index ? colors.selection.block.fg : undefined}>{` ${label} `}</Text>)}</Box>
    {status ? <Text>{status}</Text> : null}
    <Text dimColor>←/→ select · Enter confirm · Esc close</Text>
  </Box>;
}
