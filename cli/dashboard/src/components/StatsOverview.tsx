import React, { useEffect, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { runtimeUsage } from "../backend/tools.js";
import { colors } from "../theme/index.js";

type Props = { client: BackendClient; cols: number; onDismiss: () => void };

const n = (value: unknown) => Number(value || 0).toLocaleString("en-US");
const pct = (value: unknown) => `${Math.round(Number(value || 0) * 100)}%`;

export function StatsOverview({ client, cols, onDismiss }: Props) {
  const [data, setData] = useState<any>({ summary: {}, projects: [] });
  const [error, setError] = useState("");
  useInput((_input, key) => { if (key.escape) onDismiss(); });
  useEffect(() => {
    let alive = true;
    runtimeUsage(client).then((value: any) => { if (alive) setData(value || {}); })
      .catch((e: any) => { if (alive) setError(String(e?.message || e)); });
    return () => { alive = false; };
  }, [client]);
  const s = data.summary || {};
  return (
    <Box flexDirection="column" paddingX={1} width={cols}>
      <Text bold>Usage Overview</Text>
      <Text dimColor>Esc close</Text>
      {error ? <Text color={colors.danger}>{error}</Text> : null}
      <Text>Tasks             {n(s.task_count)}</Text>
      <Text>Provider calls    {n(s.provider_calls)}</Text>
      <Text>Input tokens      {n(s.input_tokens)}</Text>
      <Text>Output tokens     {n(s.output_tokens)}</Text>
      <Text>Cache read        {n(s.cache_read_tokens)}  {pct(s.cache_hit_ratio)}</Text>
      <Text>Tool calls        {n(s.tool_calls)}</Text>
      {(data.projects || []).length > 0 ? <Text color={colors.divider.color}>{"─".repeat(Math.max(10, cols - 2))}</Text> : null}
      {(data.projects || []).map((project: any) => (
        <Text key={project.project} dimColor={Boolean(project.error)}>
          {String(project.project).padEnd(22)}  in {n(project.input_tokens)}  out {n(project.output_tokens)}  cache {pct(project.cache_hit_ratio)}
        </Text>
      ))}
    </Box>
  );
}
