import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ReactNode } from "react";
import type { ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import { colors, formatDuration } from "../theme/index.js";
import { MarkdownText } from "./MarkdownText.js";
import { RuntimeNoticeRow } from "./RuntimeNoticeList.js";
import { Spinner } from "./Spinner.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";

type Props = {
  activity: StreamActivity[];
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  verbose: boolean;
  width: number;
  active?: boolean;
  elapsedMs?: number;
  providerUsage?: ProviderUsageSummary;
  renderDetail?: (ref: string, key: React.Key) => ReactNode;
};

function webFamily(tool: ToolCallCard | undefined): {key: string; label: string} | null {
  const name = String(tool?.tool_name || "").toLowerCase();
  if (name === "web_search" || name === "hosted_web_search") {
    return {key: "web", label: "Web search"};
  }
  if (name === "web_fetch") return {key: "web", label: "Web fetch"};
  return null;
}

function aggregateTool(first: ToolCallCard, rest: ToolCallCard[]): ToolCallCard {
  const all = [first, ...rest];
  const running = all.some(tool => tool.state === "running" || tool.state === "pending" || tool.is_running);
  const succeeded = all.some(tool => tool.state === "completed" && tool.allowed !== false);
  const failed = all.every(tool => tool.state === "error" || tool.allowed === false);
  return {
    ...first,
    target: "",
    result_text: undefined,
    compact_summary: undefined,
    blocked_reason: failed ? "All parallel requests failed" : undefined,
    duration_ms: Math.max(...all.map(tool => Number(tool.duration_ms || 0))),
    is_running: running,
    state: running ? "running" : failed ? "error" : succeeded ? "completed" : first.state,
    allowed: !failed,
  };
}

/** Shared ordered renderer for main/subprocess live and completed trace capsules. */
export function TurnTimeline({
  activity, tools, notices, verbose, width, active = false, elapsedMs = 0,
  providerUsage, renderDetail,
}: Props) {
  const firstReasoningIndex = activity.findIndex(item => item.kind === "reasoning");
  const usage = providerUsage
    ? ` · ${providerUsage.input_tokens} in / ${providerUsage.output_tokens} out`
    : "";

  return <Box flexDirection="column" paddingLeft={2}>
    {firstReasoningIndex < 0 && (active || providerUsage) ? <Box flexDirection="row">
      {active ? <Spinner frames={colors.spinner.thinkingFrames}
        intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/> : null}
      <Text dimColor>{active ? " Thinking… " + formatDuration(elapsedMs) : "Thinking"}{usage}</Text>
    </Box> : null}
    {activity.map((item, index) => {
      if (item.kind === "tool") {
        const family = webFamily(tools[item.toolIndex]);
        if (family) {
          const previous = activity[index - 1];
          if (previous?.kind === "tool" && webFamily(tools[previous.toolIndex])?.key === family.key) {
            return null;
          }
        }
      }
      let row: ReactNode = null;
      if (item.kind === "reasoning") {
        if (verbose || index === firstReasoningIndex) {
          row = <Box flexDirection="column">
            {index === firstReasoningIndex ? <Box flexDirection="row">
              {active ? <Spinner frames={colors.spinner.thinkingFrames}
                intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/> : null}
              <Text dimColor>{active ? " Thinking… " + formatDuration(elapsedMs) : "Thinking"}{usage}</Text>
            </Box> : null}
            {verbose ? <Box paddingLeft={2}>
              <MarkdownText content={item.text} width={Math.max(10, width - 4)} dimColor/>
            </Box> : null}
          </Box>;
        }
      } else if (item.kind === "progress") {
        if (item.visibility === "public" || verbose) {
          row = <Box flexDirection="row">
            {item.actor_label ? <Text dimColor>{item.actor_label} · </Text> : null}
            <MarkdownText content={item.text} width={Math.max(10, width - 2)} dimColor/>
          </Box>;
        }
      } else if (item.kind === "tool") {
        const tool = tools[item.toolIndex];
        const family = webFamily(tool);
        if (tool && family) {
          const siblings: ToolCallCard[] = [];
          for (let cursor = index + 1; cursor < activity.length; cursor += 1) {
            const next = activity[cursor];
            if (next?.kind !== "tool" || webFamily(tools[next.toolIndex])?.key !== family.key) break;
            const sibling = tools[next.toolIndex];
            if (sibling) siblings.push(sibling);
          }
          const labels = [tool, ...siblings].map(item => webFamily(item)?.label).filter(Boolean);
          const displayName = labels.every(label => label === labels[0]) ? family.label : "Web research";
          row = <ToolCallDisplay tool={aggregateTool(tool, siblings)} displayName={displayName}
            count={siblings.length + 1} suppressDetails expanded={false}
            width={Math.max(20, width - 2)}/>;
        } else {
          row = tool ? <ToolCallDisplay tool={tool} expanded={verbose} width={Math.max(20, width - 2)}/> : null;
        }
      } else {
        const notice = notices[item.noticeIndex];
        row = notice ? <RuntimeNoticeRow notice={notice} verbose={verbose}/> : null;
      }
      if (!row) return null;
      return <Box key={`${item.kind}:${index}`} flexDirection="column">
        {row}
        {"detail_ref" in item && item.detail_ref && verbose && renderDetail
          ? <Box paddingLeft={2}>{renderDetail(item.detail_ref, `${item.kind}:${index}:detail`)}</Box> : null}
      </Box>;
    })}
  </Box>;
}
