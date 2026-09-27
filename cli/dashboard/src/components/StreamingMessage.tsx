// src/components/StreamingMessage.tsx — shared transient streaming-row renderer.
// Renders the in-flight token stream (gutter + timestamp + live spinner + wrapped
// text + tool cards), kept separate from the persisted message list.
import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import type { StreamingRow } from "../types.js";
import { colors } from "../theme/index.js";
import { StreamingMarkdownText } from "./MarkdownText.js";
import { TurnTimeline } from "./TurnTimeline.js";
import type { StreamActivity } from "../types.js";

type Props = {
  streaming: StreamingRow;
  contentWidth: number;
  verbose?: boolean;
};

export function StreamingMessage({ streaming, contentWidth, verbose = false }: Props) {
  const [elapsedMs, setElapsedMs] = useState(0);
  useEffect(() => {
    const started = Date.parse(streaming.timestamp) || Date.now();
    const update = () => setElapsedMs(Math.max(0, Date.now() - started));
    update();
    const timer = setInterval(update, 100);
    return () => clearInterval(timer);
  }, [streaming.timestamp]);
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

  return (
    <Box flexDirection="column">
      <TurnTimeline activity={activity} tools={streaming.tools} notices={streaming.notices}
        verbose={verbose} width={contentWidth} active elapsedMs={elapsedMs}
        providerUsage={streaming.provider_usage}/>
      {showText && hasText ? (
        <Box flexDirection="row">
          <Text color={colors.chat.gutter}>● </Text>
          <StreamingMarkdownText content={streaming.text} width={Math.max(10, contentWidth - 2)}/>
        </Box>
      ) : null}
    </Box>
  );
}
