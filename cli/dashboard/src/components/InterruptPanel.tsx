import {PageActions} from "../ui/PageActions.js";
import {useCopy} from "../ui/PageFrame.js";
import React, { useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import { cancelRequest, stopProcess } from "../backend/tools.js";
import { useManagedInput } from "../input/runtime.js";
import { colors } from "../theme/index.js";
import {matchChord} from "../input/bindings.js";
import {useLocalHelpCommand} from "../hooks/useLocalHelpCommand.js";
import type {FooterConfig} from "./CommandBar.js";
const ignoreFooter=(_value:FooterConfig|null)=>{};

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

export function InterruptPanel({ client, project, processId, requestId, subtree, onDismiss,onFooter=ignoreFooter }: {
  client: BackendClient; project: string; processId: string; requestId?: string;
  subtree: boolean; onDismiss: () => void;
  onFooter?:(value:FooterConfig|null)=>void;
}) {
  const t=useCopy();
  const [selection, setSelection] = useState(1);
  const [status, setStatus] = useState("");
  const inFlight = useRef(false);
  const help=useLocalHelpCommand("interrupt",onFooter,!inFlight.current);
  const confirm = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setStatus(t({en:"Interrupting… waiting for the host acknowledgement",zh:"正在中断，等待宿主确认…"}));
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
      setStatus(t(result.tree_terminal ? {en:"Execution stopped. Conversation and committed changes preserved.",zh:"执行已停止，保留对话与已完成更改。"}
        : {en:"Interruption accepted; execution is stopping. Conversation and committed changes are preserved.",zh:"已接收中断请求，正在停止；保留对话与已完成更改。"}));
    } catch (error) { setStatus(`[INTERRUPT_FAILED] ${String(error)}`); }
    finally { inFlight.current = false; }
  };
  useManagedInput((_input, key) => {
    if(help.handle(_input,key))return;
    if (matchChord("escape",_input,key)) { onDismiss(); return; }
    if (inFlight.current) return;
    if (matchChord("left",_input,key)||matchChord("right",_input,key)) setSelection(value => 1 - value);
    if (matchChord("enter",_input,key)) { if (selection === 1) onDismiss(); else void confirm(); }
  },{clipboard:false});
  return <Box flexDirection="column" paddingX={1}>
    <Text bold>{t({en:"Interrupt ",zh:"中断 "})}{subtree ? t({en:"the main process and its active subprocesses",zh:"主进程及其活动子进程"}) : t({en:"this subprocess",zh:"当前子进程"})}?</Text>
    <Text>{t({en:"Stop the current calls; preserve the conversation and completed file changes. No automatic rollback.",zh:"停止当前调用，保留对话和已完成的文件更改，不自动回滚。"})}</Text>
    <PageActions items={[{id:0,enabled:true,label:{en:"Interrupt",zh:"中断"}},{id:1,enabled:true,label:{en:"Keep working",zh:"继续工作"}}]} selected={selection} focused/>
    {status ? <Text>{status}</Text> : null}
  </Box>;
}
