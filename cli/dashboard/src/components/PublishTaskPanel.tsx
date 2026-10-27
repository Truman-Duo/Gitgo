import React, {useEffect, useRef, useState} from "react";
import {Box, Text, type ScrollBoxHandle} from "@anthropic/ink";
import type {BackendClient} from "../backend/client.js";
import type {ProcessInfo} from "../hooks/useLoopData.js";
import type {ChatMessage} from "../types.js";
import {useManagedInput as useInput} from "../input/runtime.js";
import {matchChord} from "../input/bindings.js";
import {useCopy} from "../ui/PageFrame.js";
import {usePanelSize} from "../theme/index.js";
import type {FooterConfig} from "./CommandBar.js";
import {VirtualConversation} from "./VirtualConversation.js";
import {ProcessDetailPanel} from "./ProcessDetailPanel.js";

export type PublishTaskReference = {process_id: string; linked_task_id: string; parent_process_id?: string; parent_task_id?: string};
export function PublishTaskPanel({client, project, reference, onFooter, onDismiss}: {
  client: BackendClient; project: string; reference: PublishTaskReference;
  onFooter: (value: FooterConfig | null) => void; onDismiss: () => void;
}) {
  const t = useCopy(), {w} = usePanelSize({minWidth: 20, widthOffset: 4});
  const [snapshot, setSnapshot] = useState<any>(null), [error, setError] = useState("");
  const [parent, setParent] = useState(false), [details, setDetails] = useState(false);
  const scroll = useRef<ScrollBoxHandle>(null);
  const processId = parent ? reference.parent_process_id : reference.process_id;
  const taskId = parent ? reference.parent_task_id : reference.linked_task_id;
  const process = snapshot?.process as ProcessInfo | undefined;
  // A reused process or a missing projection must never open another task.
  const verified = snapshot?.project === project && process?.process_id === processId && process?.active_task_id === taskId;
  const messages: ChatMessage[] = !verified ? [] : (snapshot.messages ?? []).filter((row: ChatMessage) => row.turn_id === taskId);
  useEffect(() => {
    let live = true;
    setSnapshot(null); setError("");
    void client.callTool("publish.task", {project, process_id: processId, task_id: taskId}).then(value => { if (live) setSnapshot(value); })
      .catch(() => { if (live) setError(t({en: "This exact task is unavailable or could not be read. Esc returns to Publish.", zh: "该任务已不可用或暂时无法读取。Esc 返回 Publish。"})); });
    return () => { live = false; };
  }, [client, project, processId, taskId]);
  useEffect(() => {
    onFooter({hidden: true});
    return () => onFooter(null);
  }, [onFooter, t, details]);
  useInput((text, key) => {
    if (details) return;
    if (matchChord("escape", text, key)) onDismiss();
    else if (text.toLowerCase() === "d" && verified) setDetails(true);
    else if (text.toLowerCase() === "p" && reference.parent_process_id && reference.parent_task_id) { setParent(value => !value); scroll.current?.scrollTo(0); }
    else if (matchChord("up", text, key) || matchChord("down", text, key)) scroll.current?.scrollBy(matchChord("up", text, key) ? -1 : 1);
  }, {isActive: !details});
  if (details && verified) return <ProcessDetailPanel project={project} processId={processId!} processes={{[processId!]: process!}} onDismiss={() => setDetails(false)}/>;
  return <Box flexDirection="column" flexGrow={1} flexBasis={0} minHeight={0}>
    <Box flexDirection="column" flexShrink={0} paddingX={1}>
      <Text bold>{t({en: "Linked task conversation", zh: "关联任务对话"})} · {project}</Text>
      <Text wrap="truncate-end">{taskId} · {processId}</Text>
      {verified ? <Text wrap="truncate-end">{process?.display_name} · {process?.status}</Text> : null}
      {error ? <Text>{error}</Text> : !snapshot ? <Text>{t({en: "Reading saved task…", zh: "正在读取已保存任务…"})}</Text>
        : !verified ? <Text>{t({en: "This exact task is unavailable or its identity changed. Esc returns to Publish.", zh: "该任务已不可用或身份发生变化。Esc 返回 Publish。"})}</Text> : null}
    </Box>
    <VirtualConversation key={processId} messages={messages} streaming={null} scrollRef={scroll} contentWidth={Math.max(12, w - 2)}
      client={client} project={project} empty={<Text>{verified ? t({en: "No conversation recorded.", zh: "暂无对话记录。"}) : ""}</Text>}/>
    <Box flexShrink={0}><Text wrap="truncate-end">{t({en: "↑/↓ scroll · D details · P parent/task · Esc return to Publish", zh: "↑/↓ 滚动 · D 详情 · P 父任务/关联任务 · Esc 返回 Publish"})}</Text></Box>
  </Box>;
}
