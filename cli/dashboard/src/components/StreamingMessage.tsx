// src/components/StreamingMessage.tsx — shared transient streaming-row renderer.
// Renders the in-flight token stream (gutter + timestamp + live spinner + wrapped
// text + tool cards), kept separate from the persisted message list.
import React, {useMemo} from "react";
import { Box, Text } from "@anthropic/ink";
import type { StreamingRow } from "../types.js";
import { colors } from "../theme/index.js";
import { StreamingMarkdownText } from "./MarkdownText.js";
import { TurnTimeline } from "./TurnTimeline.js";
import type { StreamActivity } from "../types.js";
import {useSharedClock} from "../hooks/useSharedClock.js";
import {Disclosure} from "./Disclosure.js";
import {useCopy} from "../ui/PageFrame.js";
import {formatDuration} from "../theme/index.js";
import {geometry} from "../theme/tokens.js";
import type {BackendClient} from "../backend/client.js";
import {traceBodyReader} from "../chat/traceSource.js";
import {type ExpandedChange,useDisclosureState} from '../hooks/useDisclosureState.js';

type Props = {
  streaming: StreamingRow;
  contentWidth: number;
  verbose?: boolean;
  client?:BackendClient;
  project?:string;
  expanded?: boolean;
  onExpandedChange?: (change: ExpandedChange) => void;
  waiting?: boolean;
};

export function StreamingMessage({streaming,contentWidth,verbose=false,client,project,
  expanded: controlledExpanded,onExpandedChange,waiting=false}: Props) {
  const t=useCopy();
  const [localExpanded,setLocalExpanded]=useDisclosureState(true,'stream:'+streaming.timestamp);
  const expanded=controlledExpanded ?? localExpanded;
  const setExpanded=onExpandedChange ?? setLocalExpanded;
  const now = useSharedClock(100);
  const started = useMemo(() => Date.parse(streaming.timestamp) || Date.now(), [streaming.timestamp]);
  const elapsedMs = Math.max(0, now - started);
  const readBody=useMemo(()=>client && project ? traceBodyReader(client,project,"") : undefined,[client,project]);
  const hasText = Boolean(streaming.text);
  const publicText = streaming.visibility === "public";
  const showText = publicText || verbose;
  const activity: StreamActivity[] = streaming.activity?.length
    ? streaming.activity
    : [
        ...(streaming.reasoning ? [{kind: "reasoning" as const, text: streaming.reasoning}] : []),
        ...streaming.tools.map((_tool, toolIndex) => ({kind: "tool" as const, toolIndex})),
        ...streaming.notices.map((_notice, noticeIndex) => ({kind: "notice" as const, noticeIndex})),
      ];
  const thinkingStarted=useMemo(()=>Date.parse(streaming.thinkingStartedAt || "") || Date.now(),
    [streaming.thinkingStartedAt,streaming.thinking,streaming.providerStep]);

  return (
    <Disclosure headerInset={geometry.record.indent} contentInset={geometry.record.indent} running={!waiting}
      label={t(waiting ? {en:"Waiting for reply",zh:"等待回复"} : {en:"Running",zh:"执行中"}) + " · " + formatDuration(elapsedMs)}
      expanded={expanded} onToggle={()=>setExpanded(value=>!value)}>
      <TurnTimeline activity={activity} tools={streaming.tools} notices={streaming.notices}
        verbose={verbose} width={Math.max(1,contentWidth-geometry.record.indent)} active elapsedMs={Math.max(0,now-thinkingStarted)}
        thinking={streaming.thinking} thinkingText={streaming.thinkingText}
        stage={streaming.stage}
        readBody={readBody}
        providerUsage={streaming.provider_usage}/>
      {showText && hasText && !(verbose && activity.some(item => item.kind === "draft")) ? (
        <Box flexDirection="row">
          <Text color={colors.chat.gutter}>● </Text>
          <StreamingMarkdownText content={streaming.text} width={Math.max(1, contentWidth - geometry.record.indent - 2)}/>
        </Box>
      ) : null}
    </Disclosure>
  );
}
