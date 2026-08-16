// src/components/GovernancePanel.tsx — /runtime governance: Quality/Patterns/Feed/Releases
import React, { memo, useState, useEffect } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import {
  governanceQuality, governancePatterns, governanceFeed, governanceReleases,
} from "../mcp/tools.js";
import { resolveInlineContextKey } from "../input/overlays/inlineContext.js";
import { colors, usePanelSize } from "../theme/index.js";
import { valueLines } from "./valueTree.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  initialTab?: number;
  onDismiss: () => void;
};

const TABS = ["Quality", "Patterns", "Feed", "Releases"] as const;

export const GovernancePanel = memo(function GovernancePanel({
  client, project, initialTab = 0, onDismiss,
}: Props) {
  const [tab, setTab] = useState(initialTab);
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setData(null);
    setErr("");
    const loaders = [governanceQuality, governancePatterns, governanceFeed, governanceReleases];
    const fn = loaders[tab];
    const args = tab === 2 ? [client, project, 20] : [client, project];
    (fn as any)(...args)
      .then((r: any) => {
        if (r?.error) setErr(r.error);
        else setData(r);
      })
      .catch((e: any) => setErr(String(e.message || e)));
  }, [tab, client, project]);

  useInput((input: string, key: any) => {
    for (const a of resolveInlineContextKey(input, key)) {
      if (a.type === "dismiss") onDismiss();
      else if (a.type === "move") setTab((t) => Math.max(0, Math.min(TABS.length - 1, t + a.delta)));
    }
  });

  const { w } = usePanelSize({ minWidth: 40 });

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box flexDirection="row" marginBottom={1}>
        {TABS.map((label, i) => (
          <Box key={label} marginRight={1}>
            <Text
              color={i === tab ? colors.named.cyan : undefined}
              bold={i === tab}
              dimColor={i !== tab}
            >[{label}]</Text>
          </Box>
        ))}
        <Text dimColor>{project}</Text>
      </Box>
      <Text dimColor>{chordLabel("leftRight")} tab    {chordLabel("escape")} back</Text>

      <Box marginTop={1} flexDirection="column">
        {err ? (
          <Text color={colors.danger}>Error: {err}</Text>
        ) : !data ? (
          <Text dimColor>Loading...</Text>
        ) : (
          valueLines(data).slice(0, 60).map((line, i) => <Text key={i}>{line}</Text>)
        )}
      </Box>
    </Box>
  );
});
