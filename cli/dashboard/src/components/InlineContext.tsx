// src/components/InlineContext.tsx — v4 blueprint inline-context: tabbed Contract/Lessons/Events
// Opened via /context command, replaces screen content, Esc to dismiss
import React, { memo, useState, useEffect } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import { contractShow, lessonList, historyList } from "../backend/tools.js";
import { ContractTab } from "./ContractTab.js";
import { LessonsTab } from "./LessonsTab.js";
import { EventsTab } from "./EventsTab.js";
import type { ToolEvent } from "../hooks/useLoopData.js";
import { resolveInlineContextKey } from "../input/overlays/inlineContext.js";
import { usePanelSize } from "../theme/index.js";
import { HorizontalHeaderStrip } from "./HorizontalHeaderStrip.js";

type Props = {
  project: string;
  client: BackendClient;
  cols: number;
  toolEvents: ToolEvent[];
  initialTab?: number;
  onDismiss: () => void;
};

const TABS = ["Contract", "Lessons", "Events"] as const;

export const InlineContext = memo(function InlineContext({
  project, client, cols, toolEvents, initialTab = 0, onDismiss,
}: Props) {
  const [activeTab, setActiveTab] = useState(initialTab);
  const [contract, setContract] = useState<any>(null);
  const [lessons, setLessons] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);

  // Lazy load on tab switch
  useEffect(() => {
    if (activeTab === 0 && !contract) {
      contractShow(client, project)
        .then(setContract).catch((error) => setContract({error:String(error)}));
    } else if (activeTab === 1 && !lessons) {
      lessonList(client, project)
        .then(setLessons).catch((error) => setLessons({error:String(error)}));
    } else if (activeTab === 2 && events.length === 0) {
      historyList(client, project)
        .then((h: any) => setEvents(Array.isArray(h) ? h : h?.entries || [])).catch(() => {});
    }
  }, [activeTab, project, client, contract, lessons, events.length]);

  useInput((input: string, key: any) => {
    for (const a of resolveInlineContextKey(input, key)) {
      if (a.type === "dismiss") {
        onDismiss();
      } else if (a.type === "move") {
        setActiveTab((t) => Math.max(0, Math.min(2, t + a.delta)));
      }
    }
  });

  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });

  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1} flexGrow={1}>
      {/* Header */}
      <HorizontalHeaderStrip
        items={TABS.map(label => ({id: label, label}))}
        selected={activeTab}
        width={w}
      />
      <Text dimColor>{project}</Text>

      {/* Content */}
      <Box flexDirection="column" flexGrow={1}>
        {activeTab === 0 && <ContractTab contract={contract} width={w} />}
        {activeTab === 1 && <LessonsTab lessons={lessons} width={w} />}
        {activeTab === 2 && <EventsTab events={events} toolEvents={toolEvents} width={w} />}
      </Box>
    </Box>
  );
});
