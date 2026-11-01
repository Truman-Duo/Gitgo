import React from "react";
import { Box, Text } from "@anthropic/ink";
import type { ReactNode } from "react";
import type { ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import { colors, formatDuration } from "../theme/index.js";
import { MarkdownText } from "./MarkdownText.js";
import { RuntimeNoticeRow } from "./RuntimeNoticeList.js";
import { Spinner } from "./Spinner.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import {useCopy} from "../ui/PageFrame.js";
import type {Copy} from "../ui/pageContract.js";

type Props = {
  activity: StreamActivity[];
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  verbose: boolean;
  width: number;
  active?: boolean;
  preview?: boolean;
  elapsedMs?: number;
  providerUsage?: ProviderUsageSummary;
  renderDetail?: (ref: string, key: React.Key) => ReactNode;
};

function webFamily(tool: ToolCallCard | undefined, t: (copy: Copy) => string): {key: string; label: string} | null {
  const name = String(tool?.tool_name || "").toLowerCase();
  if (name === "web_search" || name === "hosted_web_search") {
    return {key: "web", label: t({en:"Web search",zh:"网页搜索"})};
  }
  if (name === "web_fetch") return {key: "web", label: t({en:"Web fetch",zh:"网页读取"})};
  return null;
}

function aggregateTool(first: ToolCallCard, rest: ToolCallCard[], t: (copy: Copy) => string): ToolCallCard {
  const all = [first, ...rest];
  const running = all.some(tool => tool.state === "running" || tool.state === "pending" || tool.is_running);
  const succeeded = all.some(tool => tool.state === "completed" && tool.allowed !== false);
  const failed = all.every(tool => tool.state === "error" || tool.allowed === false);
  return {
    ...first,
    target: "",
    result_text: undefined,
    compact_summary: undefined,
    blocked_reason: failed ? t({en:"All parallel requests failed",zh:"所有并行请求均失败"}) : undefined,
    duration_ms: Math.max(...all.map(tool => Number(tool.duration_ms || 0))),
    is_running: running,
    state: running ? "running" : failed ? "error" : succeeded ? "completed" : first.state,
    allowed: !failed,
  };
}

/** Shared ordered renderer for main/subprocess live and completed trace capsules. */
export function TurnTimeline({
  activity, tools, notices, verbose, width, active = false, elapsedMs = 0,
  providerUsage, renderDetail, preview = false,
}: Props) {
  const t=useCopy();
  const firstReasoningIndex = activity.findIndex(item => item.kind === "reasoning");
  const usage = providerUsage
    ? t({en:` · ${providerUsage.input_tokens} in / ${providerUsage.output_tokens} out`,zh:` · 输入 ${providerUsage.input_tokens} / 输出 ${providerUsage.output_tokens}`})
    : "";

  return <Box flexDirection="column" paddingLeft={2}>
    {firstReasoningIndex < 0 && (active || providerUsage) ? <Box flexDirection="row">
      {active ? <Spinner frames={colors.spinner.thinkingFrames}
        intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/> : null}
      <Text dimColor>{active ? t({en:" Thinking… ",zh:" 思考中… "}) + formatDuration(elapsedMs) : t({en:"Thinking",zh:"思考"})}{usage}</Text>
    </Box> : null}
    {activity.map((item, index) => {
      if (item.kind === "tool") {
        const family = webFamily(tools[item.toolIndex], t);
        if (family) {
          const previous = activity[index - 1];
          if (previous?.kind === "tool" && webFamily(tools[previous.toolIndex], t)?.key === family.key) {
            return null;
          }
        }
      }
      let row: ReactNode = null;
      if (item.kind === "reasoning") {
        if (verbose || index === firstReasoningIndex) {
          const thinkingActive = active && (!verbose || index === activity.length - 1);
          row = <Box flexDirection="column">
            {verbose || index === firstReasoningIndex ? <Box flexDirection="row">
              {thinkingActive ? <Spinner frames={colors.spinner.thinkingFrames}
                intervalMs={colors.spinner.thinkingIntervalMs} color={colors.warning}/> : null}
              <Text dimColor>{thinkingActive ? t({en:" Thinking… ",zh:" 思考中… "}) + formatDuration(elapsedMs) : t({en:"Thinking",zh:"思考"})}{index === firstReasoningIndex ? usage : ""}</Text>
            </Box> : null}
            {verbose ? <Box paddingLeft={2}>
              <MarkdownText content={item.text} width={Math.max(10, width - 4)} dimColor/>
            </Box> : null}
          </Box>;
        }
      } else if (item.kind === "draft") {
        if (verbose) row = <Box flexDirection="column"><Text dimColor>{t({en:"Response draft",zh:"回答草稿"})}</Text><Box paddingLeft={2}><MarkdownText content={item.text} width={Math.max(10,width-4)} dimColor/></Box></Box>;
      } else if (item.kind === "progress") {
        if (item.visibility === "public" || verbose) {
          row = <Box flexDirection="row">
            {item.actor_label ? <Text dimColor>{item.actor_label} · </Text> : null}
            <MarkdownText content={item.text} width={Math.max(10, width - 2)} dimColor/>
          </Box>;
        }
      } else if (item.kind === "tool") {
        const tool = tools[item.toolIndex];
        const family = webFamily(tool, t);
        if (tool && family) {
          const siblings: ToolCallCard[] = [];
          for (let cursor = index + 1; cursor < activity.length; cursor += 1) {
            const next = activity[cursor];
            if (next?.kind !== "tool" || webFamily(tools[next.toolIndex], t)?.key !== family.key) break;
            const sibling = tools[next.toolIndex];
            if (sibling) siblings.push(sibling);
          }
          const labels = [tool, ...siblings].map(item => webFamily(item, t)?.label).filter(Boolean);
          const displayName = labels.every(label => label === labels[0]) ? family.label : t({en:"Web research",zh:"网页调研"});
          row = <ToolCallDisplay tool={aggregateTool(tool, siblings, t)} displayName={displayName}
            count={siblings.length + 1} suppressDetails expanded={false}
            width={Math.max(20, width - 2)}/>;
        } else {
          const display = tool ? <ToolCallDisplay tool={tool} expanded={verbose} compactPreview={active || preview} width={Math.max(20, width - 2)}/> : null;
          row = tool?.composite_tool ? <Box flexDirection="column" marginLeft={1} paddingLeft={1}
            borderStyle="single" borderLeft borderRight={false} borderTop={false} borderBottom={false} borderColor={colors.chat.turnGuide}>
            <Text dimColor>{tool.composite_tool} / {tool.step_id || t({en:"step",zh:"步骤"})}</Text>{display}
          </Box> : display;
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
