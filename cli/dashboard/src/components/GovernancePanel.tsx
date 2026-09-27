// src/components/GovernancePanel.tsx — /runtime governance: Quality/Patterns/Feed/Releases
import React, { memo, useState, useEffect, useRef, useCallback } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import {
  governanceQuality, governancePatterns, governanceFeed, governanceReleases,
} from "../backend/tools.js";
import { resolveInlineContextKey } from "../input/overlays/inlineContext.js";
import { colors, usePanelSize } from "../theme/index.js";
import { valueLines } from "./valueTree.js";
import { chordLabel } from "../input/bindings.js";
import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  initialTab?: number;
  onDismiss: () => void;
  interactive?: boolean;
};

const TABS = ["Quality", "Patterns", "Feed", "Releases"] as const;

export const GovernancePanel = memo(function GovernancePanel({
  client, project, initialTab = 0, onDismiss, interactive = true,
}: Props) {
  const [tab, setTab] = useState(initialTab);
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");
  const [feedLoadingOlder, setFeedLoadingOlder] = useState(false);
  const feedPaged = useRef(false);
  const feedHistory = useRef<any[]>([]);

  useEffect(() => {
    let alive = true;
    let busy = false;
    setData(null);
    setErr("");
    feedPaged.current = false;
    feedHistory.current = [];
    const loaders = [governanceQuality, governancePatterns, governanceFeed, governanceReleases];
    const fn = loaders[tab];
    const args = tab === 2 ? [client, project, 20, ""] : [client, project];
    const refresh = () => {
      if (busy || !alive) return;
      busy = true;
      (fn as any)(...args)
      .then((r: any) => {
        if (!alive) return;
        if (r?.error) setErr(r.error);
        else if (tab === 2 && feedPaged.current) return;
        else { setErr(""); setData(r); }
      })
      .catch((e: any) => { if (alive) setErr(String(e.message || e)); })
      .finally(() => { busy = false; });
    };
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [tab, client, project]);

  const loadOlderFeed = useCallback(() => {
    const cursor = String(data?.page?.next_cursor || "");
    if (tab !== 2 || !data?.page?.has_more || !cursor || feedLoadingOlder) return;
    feedPaged.current = true;
    setFeedLoadingOlder(true);
    governanceFeed(client, project, 20, cursor)
      .then((next: any) => {
        feedHistory.current.push(data);
        setData(next);
      })
      .catch((e: any) => setErr(String(e?.message || e)))
      .finally(() => setFeedLoadingOlder(false));
  }, [client, project, tab, data, feedLoadingOlder]);

  const loadNewerFeed = useCallback(() => {
    if (tab !== 2 || feedLoadingOlder || feedHistory.current.length === 0) return;
    const previous = feedHistory.current.pop();
    feedPaged.current = feedHistory.current.length > 0;
    setData(previous);
  }, [tab, feedLoadingOlder]);

  useInput((input: string, key: any) => {
    for (const a of resolveInlineContextKey(input, key)) {
      if (a.type === "dismiss") onDismiss();
      else if (a.type === "move") setTab((t) => Math.max(0, Math.min(TABS.length - 1, t + a.delta)));
      else if (a.type === "page" && a.delta > 0) loadOlderFeed();
      else if (a.type === "page" && a.delta < 0) loadNewerFeed();
    }
  }, {isActive: interactive});

  const { w } = usePanelSize({ minWidth: 40 });

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <HorizontalHeaderStrip
        items={TABS.map(label => ({id: label, label}))}
        selected={tab}
        width={Math.max(10, w - 2)}
      />
      <Text dimColor>{project}</Text>
      <Text dimColor>{chordLabel("leftRight")} tab    {tab === 2 ? `${chordLabel("pageUp")} newer · ${chordLabel("pageDown")} older    ` : ""}{chordLabel("escape")} back</Text>

      <Box marginTop={1} flexDirection="column">
        {err ? (
          <Text color={colors.danger}>Error: {err}</Text>
        ) : !data ? (
          <Text dimColor>Loading...</Text>
        ) : (
          valueLines(tab === 2 ? {
            items: data.items,
            page: {
              number: feedHistory.current.length + 1,
              items: (data.items || []).length,
              has_more: Boolean(data.page?.has_more),
              scope: data.page?.scope,
              loading: feedLoadingOlder,
            },
          } : data).slice(0, 60).map((line, i) => <Text key={i}>{line}</Text>)
        )}
      </Box>
    </Box>
  );
});
