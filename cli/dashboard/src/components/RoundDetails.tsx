// Expanded details share the visible round's index and read bodies on demand.
// They never enter the provider context and never become a second event writer.
import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { BackendClient } from "../backend/client.js";
import type { ChatMessage } from "../types.js";
import type { TurnTimelineModel } from "../chat/turnTimeline.js";
import { TurnTimeline } from "./TurnTimeline.js";
import {useCopy} from "../ui/PageFrame.js";
import type {TraceHistory} from "../hooks/useTraceHistory.js";
import {FeedbackMessage} from "./FeedbackMessage.js";

export function RoundDetails({ message, client, project, verbose, width, history, timeline }: {
  message: ChatMessage; client?: BackendClient; project?: string;
  verbose: boolean; width: number; history: TraceHistory; timeline: TurnTimelineModel;
}) {
  const t=useCopy();
  const trace = history.index;
  const canRead = Boolean(client && project && message.trace_id);

  if (!canRead) return <Box flexDirection="column">
    <TurnTimeline {...timeline} verbose={verbose} width={width}/>
    {timeline.activity.length === 0
      ? <Text dimColor>{t({en:"No saved trajectory is available.",zh:"没有已保存的轨迹。"})}</Text> : null}
  </Box>;

  return <Box flexDirection="column">
    {!trace && !history.unavailable ? <Text dimColor>{t({en:"Loading trajectory…",zh:"正在读取轨迹…"})}</Text> : null}
    <TurnTimeline {...timeline} verbose={verbose} width={width} readBody={history.readBody}/>
    {trace?.complete && trace.events.length === 0
      ? <Text dimColor>{t({en:"No events are available for this agent; older traces may have expired.",zh:"此进程没有可读取事件，早期轨迹可能已过期。"})}</Text> : null}
    {history.unavailable ? <Text dimColor>{t({en:"Trajectory not fully read. Fold and reopen to retry.",zh:"轨迹尚未完整读取，收起并重新展开可重试。"})}</Text> : null}
    <FeedbackMessage feedback={history.feedback}/>
  </Box>;
}
