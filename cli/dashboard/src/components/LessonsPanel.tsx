// src/components/LessonsPanel.tsx — /runtime lesson: list/search/verify lessons
import React, { memo, useState, useEffect, useCallback } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import {
  lessonHarvestPreview, lessonHarvestResolve, lessonList, lessonSearch, lessonVerify,
} from "../backend/tools.js";
import { resolveLessonsKey } from "../input/overlays/lessons.js";
import { colors, usePanelSize, useSelectionStyle } from "../theme/index.js";
import { LessonsTab } from "./LessonsTab.js";
import { chordLabel } from "../input/bindings.js";
import type { UseTextInputReturn } from "../hooks/useTextInput.js";
import { applyTextOp } from "../hooks/useTextInput.js";
import type { FooterConfig } from "./CommandBar.js";

type Props = {
  client: BackendClient;
  project: string;
  cols: number;
  initialQuery?: string;
  onDismiss: () => void;
  interactive?: boolean;
  cmdInput?: UseTextInputReturn;
  onFooter?: (config: FooterConfig | null) => void;
};

export const LessonsPanel = memo(function LessonsPanel({
  client, project, initialQuery, onDismiss, interactive = true, cmdInput, onFooter,
}: Props) {
  const [lessons, setLessons] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<"list" | "search" | "harvest" | "confirm">(initialQuery ? "search" : "list");
  const [query, setQuery] = useState(initialQuery ?? "");
  const [searchResults, setSearchResults] = useState<any[] | null>(null);
  const [sel, setSel] = useState(0);
  const [status, setStatus] = useState("");
  const [harvestStatus, setHarvestStatus] = useState<"idle" | "analyzing" | "saving">("idle");
  const [proposal, setProposal] = useState<any>(null);
  const [proposalChoice, setProposalChoice] = useState(0);

  const refresh = useCallback(() => {
    lessonList(client, project)
      .then((r: any) => { setLessons(r); setLoading(false); })
      .catch(() => setLoading(false));
  }, [client, project]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (!onFooter) return;
    if (mode !== "harvest" || !cmdInput) {
      onFooter({hidden: true});
      return () => onFooter(null);
    }
    onFooter({
      kind: "normal", cmdInput, suggestions: [], suggestionIdx: 0, cmdResult: "",
      statusText: `${chordLabel("enter")} analyze   ${chordLabel("escape")} cancel`,
    });
    return () => onFooter(null);
  }, [mode, cmdInput, cmdInput?.value, cmdInput?.cursor, onFooter]);

  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      lessonSearch(client, project, initialQuery.trim())
        .then((r: any) => setSearchResults(Array.isArray(r) ? r : r?.lessons || []))
        .catch(() => setSearchResults([]));
    }
  }, [initialQuery, client, project]);

  useInput((input: string, key: any) => {
    if (mode === "harvest") {
      if (key.escape) {
        cmdInput?.setValue(""); setMode("list"); setHarvestStatus("idle");
      } else if (key.return && cmdInput?.value.trim() && harvestStatus === "idle") {
        setHarvestStatus("analyzing"); setStatus("");
        lessonHarvestPreview(client, project, cmdInput.value.trim())
          .then((value: any) => {
            setProposal(value); setProposalChoice(0); setMode("confirm");
            setHarvestStatus("idle"); cmdInput.setValue("");
          })
          .catch((error: any) => {
            setStatus(String(error?.message || error)); setHarvestStatus("idle");
          });
      } else if (cmdInput) {
        if (key.backspace || key.delete) applyTextOp({op: "delete_back"}, cmdInput);
        else if (key.leftArrow) applyTextOp({op: "move_cursor", delta: -1}, cmdInput);
        else if (key.rightArrow) applyTextOp({op: "move_cursor", delta: 1}, cmdInput);
        else if (input && !key.ctrl && !key.meta) applyTextOp({op: "insert", text: input}, cmdInput);
      }
      return;
    }
    if (mode === "confirm") {
      if (key.escape) { setProposal(null); setMode("list"); return; }
      if (key.leftArrow || key.upArrow) setProposalChoice(0);
      else if (key.rightArrow || key.downArrow) setProposalChoice(1);
      else if (key.return && proposal?.proposal_id && harvestStatus === "idle") {
        setHarvestStatus("saving");
        const action = proposalChoice === 0 ? "accept" : "discard";
        lessonHarvestResolve(client, project, proposal.proposal_id, action)
          .then((value: any) => {
            setStatus(action === "accept"
              ? `Knowledge harvested · ${Number(value?.count || 0)} pending`
              : "Knowledge proposal discarded");
            setProposal(null); setMode("list"); setHarvestStatus("idle"); refresh();
          })
          .catch((error: any) => {
            setStatus(String(error?.message || error)); setHarvestStatus("idle");
          });
      }
      return;
    }
    for (const a of resolveLessonsKey(mode, input, key)) {
      switch (a.type) {
        case "dismiss": onDismiss(); break;
        case "move":
          setSel((s) => Math.max(0, Math.min((lessons?.pending?.length || 1) - 1, s + a.delta)));
          break;
        case "searchMode": setMode("search"); setQuery(""); setSearchResults(null); break;
        case "harvestMode":
          if (cmdInput) { cmdInput.setValue(""); setStatus(""); setMode("harvest"); }
          else setStatus("Explicit harvest requires the NormalBar-enabled runtime view.");
          break;
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
              .then((r: any) => setSearchResults(Array.isArray(r) ? r : r?.lessons || []))
              .catch(() => setSearchResults([]));
          }
          break;
        case "searchBackspace": setQuery((q) => q.slice(0, -1)); break;
        case "searchInsert": setQuery((q) => q + a.text); break;
      }
    }
  }, {isActive: interactive});

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

  if (mode === "harvest") {
    return <Box flexDirection="column" padding={1} width={w}>
      <Text bold>Harvest a lesson — {project}</Text>
      <Text dimColor>Describe the concrete situation, reusable action, and expected evidence.</Text>
      <Box marginTop={1}>
        <Text color={harvestStatus === "analyzing" ? colors.warning : undefined}>
          {harvestStatus === "analyzing" ? "Analyzing the observation semantically…" : "Input is in the NormalBar below."}
        </Text>
      </Box>
      {status ? <Text color={colors.danger}>{status}</Text> : null}
    </Box>;
  }

  if (mode === "confirm" && proposal) {
    const candidates = Array.isArray(proposal.candidates) ? proposal.candidates : [];
    return <Box flexDirection="column" padding={1} width={w}>
      <Text bold>{String(proposal.question || "Save proposed lessons?")}</Text>
      <Box flexDirection="column" marginTop={1}>
        {candidates.map((candidate: any, index: number) => <Box key={candidate.id || index} flexDirection="column">
          <Text>{candidate.trigger || "Reusable situation"}</Text>
          <Box paddingLeft={2}><Text dimColor>{candidate.rule || ""}</Text></Box>
        </Box>)}
      </Box>
      <Box marginTop={1} gap={2}>
        {["Save pending", "Discard"].map((label, index) => {
          const style = useSelectionStyle(index === proposalChoice ? "focused" : "non-focused", "block");
          return <Text key={label} color={style.fg} backgroundColor={style.bg} bold={style.bold}> {label} </Text>;
        })}
      </Box>
      <Text dimColor>{proposalChoice === 0
        ? "Searchable and injectable immediately; verification remains separate."
        : "No lesson is saved; the decision remains auditable."}</Text>
      {harvestStatus === "saving" ? <Text dimColor>Saving decision…</Text> : null}
    </Box>;
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
        <Text dimColor>S search    H harvest    V verify</Text>
        {selLesson ? (
          <Text dimColor>    sel: {selLesson.id?.slice(0, 12) || "?"}  {selLesson.rule?.slice(0, w - 40) || ""}</Text>
        ) : null}
        <Text dimColor>{chordLabel("upDown")} select    {chordLabel("escape")} back</Text>
      </Box>
    </Box>
  );
});
