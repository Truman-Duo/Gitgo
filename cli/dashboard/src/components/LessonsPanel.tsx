// src/components/LessonsPanel.tsx — /runtime lesson: list/search/verify lessons
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { lessonList, lessonSearch, lessonVerify } from "../mcp/tools.js";
import { resolveLessonsKey } from "../input/overlays/lessons.js";
import { colors, usePanelSize, useSelectionStyle } from "../theme/index.js";
import { LessonsTab } from "./LessonsTab.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  initialQuery?: string;
  onDismiss: () => void;
};

export const LessonsPanel = memo(function LessonsPanel({ client, project, initialQuery, onDismiss }: Props) {
  const [lessons, setLessons] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<"list" | "search">(initialQuery ? "search" : "list");
  const [query, setQuery] = useState(initialQuery ?? "");
  const [searchResults, setSearchResults] = useState<any[] | null>(null);
  const [sel, setSel] = useState(0);
  const [status, setStatus] = useState("");

  const refresh = useCallback(() => {
    lessonList(client, project)
      .then((r: any) => { setLessons(r); setLoading(false); })
      .catch(() => setLoading(false));
  }, [client, project]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      lessonSearch(client, project, initialQuery.trim())
        .then((r: any) => setSearchResults(r?.lessons || []))
        .catch(() => setSearchResults([]));
    }
  }, [initialQuery, client, project]);

  useInput((input: string, key: any) => {
    for (const a of resolveLessonsKey(mode, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "move":
          setSel((s) => Math.max(0, Math.min((lessons?.pending?.length || 1) - 1, s + a.delta)));
          break;
        case "searchMode": setMode("search"); setQuery(""); setSearchResults(null); break;
        case "verify": {
          const pending = lessons?.pending || [];
          const it = pending[sel];
          if (it?.id) {
            lessonVerify(client, project, it.id)
              .then((r: any) => {
                setStatus(r?.verified ? `Verified ${it.id.slice(0, 12)}` : String(r?.reason || "not found"));
                refresh();
              })
              .catch((e: any) => setStatus(String(e.message || e)));
          }
          break;
        }
        case "searchBack": setMode("list"); setQuery(""); setSearchResults(null); break;
        case "searchRun":
          if (query.trim()) {
            lessonSearch(client, project, query.trim())
              .then((r: any) => setSearchResults(r?.lessons || []))
              .catch(() => setSearchResults([]));
          }
          break;
        case "searchBackspace": setQuery((q) => q.slice(0, -1)); break;
        case "searchInsert": setQuery((q) => q + a.text); break;
      }
    }
  });

  const { w } = usePanelSize({ minWidth: 40 });

  // Search mode
  if (mode === "search") {
    return (
      <Box flexDirection="column" padding={1} width={w}>
        <Box marginBottom={1}>
          <Text bold>Lesson Search: {project}</Text>
          <Text dimColor>    {chordLabel("enter")} search    {chordLabel("escape")} back</Text>
        </Box>
        <Box>
          <Text>query: {query || "_"}</Text>
        </Box>
        <Box marginTop={1} flexDirection="column">
          {searchResults === null ? (
            <Text dimColor>Type a query and press Enter.</Text>
          ) : searchResults.length === 0 ? (
            <Text dimColor>No matching lessons.</Text>
          ) : (
            searchResults.slice(0, 20).map((l: any, i: number) => {
              const st = useSelectionStyle("non-focused", "block", "accent");
              const rule = l.rule || l.trigger || l.id || "?";
              const sev = l.severity || "medium";
              return (
                <Box key={i} flexDirection="row">
                  <Text color={st.fg}>[{sev.slice(0, 1).toUpperCase()}]</Text>
                  <Text dimColor> {rule.slice(0, w - 12)}</Text>
                </Box>
              );
            })
          )}
        </Box>
      </Box>
    );
  }

  if (loading) return <Box padding={1}><Text dimColor>Loading lessons...</Text></Box>;

  const pending = lessons?.pending || [];
  const selLesson = pending[sel];

  return (
    <Box flexDirection="column" padding={1} width={w}>
      <Box marginBottom={1}>
        <Text bold>Lessons: {project}</Text>
        <Text dimColor>    {pending.length} pending</Text>
      </Box>
      {status ? <Text color={colors.named.green}>{status}</Text> : null}

      <LessonsTab lessons={lessons} width={w} />

      <Box marginTop={1}>
        <Text dimColor>S search    V verify</Text>
        {selLesson ? (
          <Text dimColor>    sel: {selLesson.id?.slice(0, 12) || "?"}  {selLesson.rule?.slice(0, w - 40) || ""}</Text>
        ) : null}
        <Text dimColor>{chordLabel("upDown")} select    {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
