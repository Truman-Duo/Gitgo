// src/components/GovernancePanel.tsx — /runtime governance: Quality/Patterns/Feed/Releases
import React, { memo, useState, useEffect, useRef, useCallback } from "react";
import { Box, ScrollBox, Text, type ScrollBoxHandle } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import {
  governanceQuality, governancePatterns, governanceFeed, governanceReleases,
} from "../backend/tools.js";
import { resolveInlineContextKey } from "../input/overlays/inlineContext.js";
import { colors, usePanelSize } from "../theme/index.js";
import { governanceSections } from "./governancePresentation.js";
import { matchChord } from "../input/bindings.js";
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
  const scrollRef = useRef<ScrollBoxHandle>(null);
  const generation = useRef(0);
  const [tab, setTab] = useState(initialTab);
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState("");
  const [feedLoadingOlder, setFeedLoadingOlder] = useState(false);
  const feedPaged = useRef(false);
  const feedHistory = useRef<any[]>([]);

  useEffect(() => {
    let alive = true;
    generation.current++;
    setFeedLoadingOlder(false);
    scrollRef.current?.scrollTo(0);
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
    return () => { alive = false; generation.current++; clearInterval(timer); };
  }, [tab, client, project]);

  const loadOlderFeed = useCallback(() => {
    const cursor = String(data?.page?.next_cursor || "");
    if (tab !== 2 || !data?.page?.has_more || !cursor || feedLoadingOlder) return;
    feedPaged.current = true;
    setFeedLoadingOlder(true);
    const requestGeneration = generation.current;
    governanceFeed(client, project, 20, cursor)
      .then((next: any) => {
        if (requestGeneration !== generation.current) return;
        feedHistory.current.push(data);
        scrollRef.current?.scrollTo(0);
        setData(next);
      })
      .catch((e: any) => { if (requestGeneration === generation.current) setErr(String(e?.message || e)); })
      .finally(() => { if (requestGeneration === generation.current) setFeedLoadingOlder(false); });
  }, [client, project, tab, data, feedLoadingOlder]);

  const loadNewerFeed = useCallback(() => {
    if (tab !== 2 || feedLoadingOlder || feedHistory.current.length === 0) return;
    const previous = feedHistory.current.pop();
    feedPaged.current = feedHistory.current.length > 0;
    setData(previous);
    scrollRef.current?.scrollTo(0);
  }, [tab, feedLoadingOlder]);

  useInput((input: string, key: any) => {
    if (matchChord("up", input, key)) { scrollRef.current?.scrollBy(-1); return; }
    if (matchChord("down", input, key)) { scrollRef.current?.scrollBy(1); return; }
    for (const a of resolveInlineContextKey(input, key)) {
      if (a.type === "dismiss") onDismiss();
      else if (a.type === "move") setTab((t) => Math.max(0, Math.min(TABS.length - 1, t + a.delta)));
      else if (a.type === "page") {
        if (tab !== 2) scrollRef.current?.scrollBy(a.delta * 10);
        else if (a.delta > 0) loadOlderFeed();
        else loadNewerFeed();
      }
    }
  }, {isActive: interactive});

  const { w } = usePanelSize({ minWidth: 20, widthOffset: 4 });

  const sections = data ? governanceSections(tab, data) : [];

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} width={w} flexGrow={1}>
      <HorizontalHeaderStrip
        items={TABS.map(label => ({id: label, label}))}
        selected={tab} width={Math.max(10, w - 2)} contentFocused={!interactive}
      />
      <Text dimColor>{project}  ·  {interactive ? "←/→ tab · ↑/↓ scroll · Esc back" : "Enter to browse"}</Text>
      {tab === 2 ? <Text dimColor>Page {feedHistory.current.length + 1} · {(data?.items || []).length} events · PgUp newer · PgDn older{feedLoadingOlder ? " · Loading…" : data?.page?.has_more ? "" : " · End of history"}</Text> : null}
      <ScrollBox ref={scrollRef} marginTop={1} flexDirection="column" flexGrow={1}>
        {err ? <Text color={colors.danger}>Error: {err}</Text>
          : !data ? <Text dimColor>Loading...</Text>
          : sections.length === 0 ? <Text dimColor>{tab === 2 ? "No governance activity recorded yet." : "No releases recorded yet."}</Text>
          : sections.map((section, index) => <Box key={index} flexDirection="column" marginBottom={1}>
              <Text bold>{section.title}</Text>
              {section.lines.length ? section.lines.map((line, i) => <Text key={i} wrap="wrap">{line}</Text>)
                : <Text dimColor>No observations yet.</Text>}
            </Box>)}
      </ScrollBox>
    </Box>
  );
});
