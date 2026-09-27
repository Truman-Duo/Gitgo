import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../input/runtime.js";
import type { BackendClient } from "../backend/client.js";
import {
  btwAsk, cancelBtw, saveBtwNote, sendRuntimeFeedback, stopProcess,
} from "../backend/tools.js";
import { initStreamState, reduceStreamEvent, type StreamState } from "../daemon/streamReducer.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { useTextInput } from "../hooks/useTextInput.js";
import { colors } from "../theme/index.js";
import { TextInput } from "./TextInput.js";
import { ToolCallDisplay } from "./ToolCallDisplay.js";
import type { ProcessInfo } from "../hooks/useLoopData.js";

type Props = {
  client: BackendClient; project: string; processId: string;
  question: string; answer: string; reasoning?: string; sidecarId: string;
  processes: Record<string, ProcessInfo>;
  verbose?: boolean;
  onDismiss: () => void; onApply: (text: string) => void;
};

const CHOICES = ["Close", "Keep note", "Apply at boundary", "Interrupt & apply"];

export function buildBtwSources(
  processes: Record<string, ProcessInfo>, currentProcessId = "",
): ProcessInfo[] {
  const all = Object.values(processes).filter((item) => !item.archived);
  const roots = all.filter((item) => !item.parent_id)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const workers = all.filter((item) => !!item.parent_id)
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  const chosenRoots = [
    ...roots.filter((item) => item.process_id === currentProcessId),
    ...roots.slice(0, 1),
  ];
  return [
    ...new Map([...chosenRoots, ...workers].map((item) => [item.process_id, item])).values(),
  ];
}

export function BtwPanel(props: Props) {
  const [selection, setSelection] = useState(0);
  const [focus, setFocus] = useState<"sources" | "input" | "choices">("sources");
  const draft = useTextInput("");
  const [answer, setAnswer] = useState(props.answer);
  const [reasoning, setReasoning] = useState(props.reasoning || "");
  const [sidecarId, setSidecarId] = useState(props.sidecarId);
  const [history, setHistory] = useState<Array<{ role: "user" | "assistant"; content: string }>>([]);
  const historyRef = useRef(history);
  const [stream, setStream] = useState<StreamState>(() => initStreamState());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const initialStarted = useRef(false);
  const sources = useMemo(
    () => buildBtwSources(props.processes, props.processId),
    [props.processes, props.processId],
  );
  const [sourceIndex, setSourceIndex] = useState(0);
  const [selectedSources, setSelectedSources] = useState<string[]>(() =>
    props.processId ? [props.processId] : [],
  );
  const [scopeConfirmed, setScopeConfirmed] = useState(false);

  useEffect(() => { historyRef.current = history; }, [history]);

  const ask = useCallback((question: string) => {
    const clean = question.trim();
    if (!clean || busy) return;
    const prior = historyRef.current;
    setBusy(true);
    setError("");
    setAnswer("");
    setReasoning("");
    setStream(initStreamState());
    void btwAsk(
      props.client, props.project, clean, sidecarId, prior, selectedSources,
      (raw) => setStream((current) => reduceStreamEvent(current, raw as StreamEvent)),
    ).then((value: any) => {
      const next = String(value?.answer || "");
      setAnswer(next);
      setReasoning(String(value?.reasoning_content || ""));
      setSidecarId(String(value?.sidecar_id || sidecarId));
      setHistory((old) => [
        ...old,
        { role: "user", content: clean },
        { role: "assistant", content: next },
      ]);
    }).catch((caught: any) => {
      setError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => setBusy(false));
  }, [busy, props.client, props.project, selectedSources, sidecarId]);

  useEffect(() => {
    if (initialStarted.current || !scopeConfirmed) return;
    initialStarted.current = true;
    ask(props.question);
  }, [ask, props.question, scopeConfirmed]);

  const finishChoice = useCallback(() => {
    const visibleAnswer = answer || stream.text;
    const applyText = `BTW discussion:\nQuestion: ${props.question}\nConclusion: ${visibleAnswer}`;
    setBusy(true);
    void (async () => {
      if (selection === 1) {
        await saveBtwNote(props.client, props.project, sidecarId, applyText);
      } else if (selection === 2 && props.processId) {
        await sendRuntimeFeedback(props.client, props.project, props.processId, applyText);
      } else if (selection === 2) {
        props.onApply(applyText);
      } else if (selection === 3) {
        if (props.processId) await stopProcess(props.client, props.project, props.processId);
        props.onApply(applyText);
      }
      props.onDismiss();
    })().catch((caught: any) => {
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy(false);
    });
  }, [answer, stream.text, selection, props, sidecarId]);

  useInput((input, key) => {
    if (key.escape) {
      if (busy && sidecarId) void cancelBtw(props.client, props.project, sidecarId).catch(() => undefined);
      props.onDismiss();
      return;
    }
    if (key.tab) {
      setFocus((value) => value === "sources" ? "input" : value === "input" ? "choices" : "sources");
      return;
    }
    if (focus === "sources") {
      if (key.leftArrow) setSourceIndex((value) => Math.max(0, value - 1));
      else if (key.rightArrow) setSourceIndex((value) => Math.min(sources.length - 1, value + 1));
      else if (input === " " && sources[sourceIndex]) {
        const id = sources[sourceIndex].process_id;
        setSelectedSources((current) => current.includes(id)
          ? current.filter((item) => item !== id)
          : [...current, id].slice(0, 8));
      } else if (key.return && !busy) {
        if (!selectedSources.length && sources[sourceIndex]) {
          setSelectedSources([sources[sourceIndex].process_id]);
        }
        setScopeConfirmed(true);
        setFocus("input");
      }
      return;
    }
    if (focus === "choices") {
      if (key.leftArrow) {
        setSelection((value) => (value + CHOICES.length - 1) % CHOICES.length);
      } else if (key.rightArrow) {
        setSelection((value) => (value + 1) % CHOICES.length);
      } else if (key.upArrow) {
        setFocus("input");
      } else if (key.return && !busy) {
        finishChoice();
      }
      return;
    }

    if (key.leftArrow) { draft.moveCursor(-1); return; }
    if (key.rightArrow) { draft.moveCursor(1); return; }
    if (key.home) { draft.moveToStart(); return; }
    if (key.end) { draft.moveToEnd(); return; }
    if (key.backspace) { draft.deleteBack(); return; }
    if (key.delete) { draft.deleteForward(); return; }
    if (key.return) {
      if (draft.value.trim() && !busy) {
        const question = draft.value.trim();
        draft.setValue("");
        ask(question);
      } else if (!draft.value.trim()) {
        setFocus("choices");
      }
      return;
    }
    if (!key.ctrl && !key.meta && input) draft.insertText(input);
  });

  const visibleAnswer = stream.text || answer;
  const visibleReasoning = stream.reasoning || reasoning;
  const latestTool = stream.tools[stream.tools.length - 1];

  return (
    <Box flexDirection="column" paddingX={1} marginTop={1}>
      <Text bold color={colors.accent}>BTW · parent-aware, isolated, read-only</Text>
      <Box flexDirection="row" gap={1}>
        <Text dimColor>View</Text>
        {sources.length ? sources.map((source, index) => {
          const selected = selectedSources.includes(source.process_id);
          const active = focus === "sources" && index === sourceIndex;
          const label = source.parent_id
            ? (source.display_name || `B${index + 1}`)
            : "A";
          return <Text key={source.process_id}
            bold={active}
            color={active ? colors.selection.block.fg : selected ? colors.accent : undefined}
            backgroundColor={active ? colors.selection.block.bg : undefined}
            dimColor={!active && !selected}>{` ${selected ? "●" : "○"} ${label} `}</Text>;
        }) : <Text dimColor> project only </Text>}
      </Box>
      {visibleReasoning && props.verbose ? <Text dimColor>{visibleReasoning}</Text> : null}
      {visibleAnswer ? <Text>{visibleAnswer}</Text> : (
        <Text dimColor>{busy
          ? `Thinking…${latestTool ? ` · ${latestTool.tool_name}` : ""}`
          : scopeConfirmed ? "No answer" : "Select main-process and subprocess views for this discussion"}</Text>
      )}
      {latestTool && !props.verbose ? (
        <Text dimColor>
          {latestTool.is_running ? "Using" : latestTool.allowed ? "Used" : "Tool failed"} · {latestTool.tool_name}
        </Text>
      ) : null}
      {props.verbose && stream.tools.length > 0 ? (
        <Box flexDirection="column">
          {stream.tools.map((tool, index) => <ToolCallDisplay
            key={tool.tool_call_id || index} tool={tool}
            width={Math.max(20, (process.stdout.columns || 80) - 6)} />)}
        </Box>
      ) : null}
      {error ? <Text color={colors.danger}>{error}</Text> : null}

      <Box marginTop={1} flexDirection="row">
        <Text color={focus === "input" ? colors.accent : undefined}>▸ </Text>
        <TextInput
          value={draft.value}
          cursorOffset={draft.cursor}
          placeholder={busy ? "Side discussion is working…" : "Follow-up"}
          focus={focus === "input"}
          showCursor={focus === "input" && !busy}
          maxWidth={Math.max(20, (process.stdout.columns || 80) - 6)}
        />
      </Box>
      <Box marginTop={1} flexDirection="row" gap={1}>
        {CHOICES.map((choice, index) => {
          const active = focus === "choices" && index === selection;
          return (
            <Text key={choice} bold={active}
              color={active ? colors.selection.block.fg : undefined}
              backgroundColor={active ? colors.selection.block.bg : undefined}
              dimColor={!active}>{` ${choice} `}</Text>
          );
        })}
      </Box>
      <Text dimColor>
        {focus === "sources" ? "Space include · Enter open discussion · " : "Enter ask · Tab switch view/input/actions · "}
        ←/→ move · Esc {busy ? "cancel & close" : "close"}
      </Text>
    </Box>
  );
}
