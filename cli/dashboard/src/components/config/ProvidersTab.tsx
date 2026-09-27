// src/components/config/ProvidersTab.tsx — /config Providers tab (self-contained).
// Owns: LLM provider list/edit/switch/delete/test + dual-column detail view.
// COMMAND input for /new /edit /delete /test /switch.

import React, { memo, useState, useEffect, useCallback, useMemo } from "react";
import { Box, Text } from "@anthropic/ink";
import { useManagedInput as useInput } from "../../input/runtime.js";
import type { LLMProvider } from "../../hooks/useLLMConfig.js";
import { useLLMConfig } from "../../hooks/useLLMConfig.js";
import { llmTest } from "../../backend/tools.js";
import {
  isCommandMode,
  resolveCommandKeys,
  applyCommandAction,
  type CommandAction,
  type CommandHandlers,
} from "../../input/commandInput.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { applyTextOp } from "../../hooks/useTextInput.js";
import { colors, truncate, useSelectionStyle } from "../../theme/index.js";
import type { ConfigTabProps } from "./types.js";
import type { Suggestion } from "../CommandBar.js";

type Mode = "list" | "edit";
type ProvidersView = "projects" | "detail";
type FocusCol = "main" | "failover";

const FIELDS = ["name", "base_url", "api_key", "model_id", "protocol", "context_window", "max_output_tokens"] as const;
const FIELD_LABELS = ["Name", "Base URL", "API Key", "Model ID", "Protocol", "Context", "Max Output"] as const;
const CONTEXT_PRESETS = [256_000, 512_000, 1_000_000, 2_000_000] as const;
type EditForm = { name: string; base_url: string; api_key: string; model_id: string; protocol: NonNullable<LLMProvider["protocol"]>; context_window: string; max_output_tokens: string };

type ProvidersCtx = {
  mode: Mode;
  providersView: ProvidersView;
  focusCol: FocusCol;
  cmdValue: string;
  suggestionCount: number;
  activeSuggestion: string;
  activeSuggestionMode: "execute" | "fill";
  editFieldIdx: number;
};

type ProvidersAction =
  | { type: "editCancel" }
  | { type: "editNextField" }
  | { type: "editPrevField" }
  | { type: "editAdvance" }
  | { type: "editText"; op: "backspace" | "delete" | "left" | "right" | "home" | "end" | "insert"; text?: string }
  | { type: "editChoice"; delta: -1 | 1 }
  | { type: "esc" }
  | { type: "command"; action: CommandAction }
  | { type: "detailLeft" }
  | { type: "detailRight" }
  | { type: "detailMainMove"; delta: number }
  | { type: "detailMainConfirm" }
  | { type: "detailFoMove"; delta: number }
  | { type: "detailFoConfirm" }
  | { type: "detailOpen" }
  | { type: "tabPrev" }
  | { type: "tabNext" };

function resolveProvidersKey(ctx: ProvidersCtx, input: string, key: any): ProvidersAction[] {
  // Edit mode
  if (ctx.mode === "edit") {
    if (matchChord("escape", input, key)) return [{ type: "editCancel" }];
    if (key.shiftTab) return [{ type: "editPrevField" }];
    if (matchChord("tabAny", input, key)) return [{ type: "editNextField" }];
    if (matchChord("enter", input, key)) return [{ type: "editAdvance" }];
    const field = FIELDS[ctx.editFieldIdx];
    if ((field === "protocol" || field === "context_window") && matchChord("left", input, key)) return [{ type: "editChoice", delta: -1 }];
    if ((field === "protocol" || field === "context_window") && matchChord("right", input, key)) return [{ type: "editChoice", delta: 1 }];
    if (matchChord("backspace", input, key)) return [{ type: "editText", op: "backspace" }];
    if (matchChord("delete", input, key)) return [{ type: "editText", op: "delete" }];
    if (matchChord("left", input, key)) return [{ type: "editText", op: "left" }];
    if (matchChord("right", input, key)) return [{ type: "editText", op: "right" }];
    if (matchChord("home", input, key)) return [{ type: "editText", op: "home" }];
    if (matchChord("end", input, key)) return [{ type: "editText", op: "end" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "editText", op: "insert", text: input }];
    }
    return [];
  }

  // Esc (list mode)
  if (matchChord("escape", input, key)) return [{ type: "esc" }];

  // COMMAND mode
  if (isCommandMode(ctx.cmdValue, input)) {
    return resolveCommandKeys(
      ctx.cmdValue, ctx.suggestionCount, input, key,
      ctx.activeSuggestion, ctx.activeSuggestionMode,
    ).map((a) => ({ type: "command", action: a }));
  }

  // Detail view (dual column) — left/right = column switch
  if (ctx.providersView === "detail") {
    if (matchChord("left", input, key)) return [{ type: "detailLeft" }];
    if (matchChord("right", input, key)) return [{ type: "detailRight" }];
    if (ctx.focusCol === "main") {
      if (matchChord("up", input, key)) return [{ type: "detailMainMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "detailMainMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "detailMainConfirm" }];
    } else {
      if (matchChord("up", input, key)) return [{ type: "detailFoMove", delta: -1 }];
      if (matchChord("down", input, key)) return [{ type: "detailFoMove", delta: 1 }];
      if (matchChord("enter", input, key)) return [{ type: "detailFoConfirm" }];
    }
    return [];
  }

  // Projects view — enter opens detail
  if (matchChord("enter", input, key)) return [{ type: "detailOpen" }];
  return [];
}

export const ProvidersTab = memo(function ProvidersTab({
  client, cmdInput, onFooter, onStatusUpdate, report, shell, contentFocused,
}: ConfigTabProps) {
  const {
    providers, activeProvider,
    loading, saveProvider, switchProvider, deleteProvider, fetchStatus,
  } = useLLMConfig(client);

  const [mode, setMode] = useState<Mode>("list");
  const [providersView, setProvidersView] = useState<ProvidersView>("projects");
  const [focusCol, setFocusCol] = useState<FocusCol>("main");
  const [mainSelIdx, setMainSelIdx] = useState(0);
  const [failoverSelIdx, setFailoverSelIdx] = useState(0);

  const [editForm, setEditForm] = useState<EditForm>({ name: "", base_url: "", api_key: "", model_id: "", protocol: "openai_chat", context_window: "128000", max_output_tokens: "4096" });
  const [editFieldIdx, setEditFieldIdx] = useState(0);
  const [editId, setEditId] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  const [suggestionIdx, setSuggestionIdx] = useState(0);
  // Provider forms follow the same restrained gray/white row focus language
  // as the rest of /config.  `edit-field` is a black-on-gray block token; the
  // old renderer applied only its foreground and made the active row nearly
  // invisible in the formal dark terminal.
  const focusedEditStyle = useSelectionStyle("focused", "row");
  const unfocusedEditStyle = useSelectionStyle("non-focused", "row");
  const defaultRowStyle = useSelectionStyle(
    contentFocused ? "focused" : "non-focused", "row",
  );
  const PROTOCOL_OPTIONS: EditForm["protocol"][] = ["auto", "openai_responses", "openai_chat", "anthropic_messages"];

  const suggestions = useMemo<Suggestion[]>(() => {
    if (!cmdInput.value.startsWith("/")) return [];
    const prefix = cmdInput.value.slice(1).toLowerCase();
    const commands: Suggestion[] = [
      {label: "/new", description: "Create provider", inputMode: "execute"},
      {label: "/edit", description: "Edit selected provider", inputMode: "execute"},
      {label: "/delete", description: "Delete selected provider", inputMode: "execute"},
      {label: "/test", description: "Test selected provider", inputMode: "execute"},
      {label: "/switch", description: "Use selected provider", inputMode: "execute"},
    ];
    return commands.filter((command) => command.label.slice(1).startsWith(prefix));
  }, [cmdInput.value]);
  const suggestionLabels = useMemo(() => suggestions.map(item => item.label), [suggestions]);

  useEffect(() => { fetchStatus(); }, [fetchStatus]);

  // Report sub/fullscreen to shell (tab bar detail + hide-on-edit).
  useEffect(() => {
    report({ sub: providersView === "detail", fullscreen: mode === "edit" });
  }, [report, providersView, mode]);

  const activeEditField = FIELDS[editFieldIdx];
  const editUsesNormalBar = mode === "edit" && activeEditField !== "protocol" && activeEditField !== "context_window";

  // Fixed operations use CommandBar; values use NormalBar; finite protocol and
  // context choices stay in the panel and use left/right.
  useEffect(() => {
    if (!contentFocused || (mode === "edit" && !editUsesNormalBar)) {
      onFooter({ hidden: true });
      return () => onFooter(null);
    }
    if (mode === "edit") {
      onFooter({
        kind: "normal", cmdInput,
        statusText: `${chordLabel("enter")} next/save · ${chordLabel("tab")} next · ${chordLabel("escape")} cancel`,
        suggestions: [], suggestionIdx: 0, cmdResult: "",
      });
      return () => onFooter(null);
    }
    const statusParts = [statusMsg, testResult].filter(Boolean) as string[];
    onFooter({
      kind: "command",
      cmdInput,
      statusText: statusParts.length > 0
        ? statusParts.join(" | ")
        : `Type ${chordLabel("slash")} for commands`,
      suggestions,
      suggestionIdx,
      cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput.value, cmdInput.cursor, statusMsg, testResult, mode, editUsesNormalBar,
      suggestions, suggestionIdx, onFooter, cmdInput, contentFocused]);

  useEffect(() => {
    if (!loading && onStatusUpdate) {
      const activeName = providers.find((p) => p.id === activeProvider)?.name || "none";
      onStatusUpdate(
        `● ${activeName} active  |  ${providers.length} providers  |  automatic failover unavailable`
      );
    }
  }, [loading, providers, activeProvider, onStatusUpdate]);

  const clampSel = useCallback((idx: number, max: number) => {
    return Math.max(0, Math.min(idx, Math.max(0, max)));
  }, []);

  const openEdit = useCallback((p?: LLMProvider) => {
    if (p) {
      // Secrets are never round-tripped from the backend. Empty means retain
      // the existing key; entering text explicitly replaces it.
      setEditForm({ name: p.name, base_url: p.base_url, api_key: "", model_id: p.model_id, protocol: p.protocol || "openai_chat", context_window: String(p.context_window || 128000), max_output_tokens: String(p.max_output_tokens || 4096) });
      setEditId(p.id);
    } else {
      setEditForm({ name: "", base_url: "", api_key: "", model_id: "", protocol: "openai_chat", context_window: "128000", max_output_tokens: "4096" });
      setEditId("");
    }
    setEditFieldIdx(0);
    cmdInput.setValue(p?.name || "");
    setMode("edit");
  }, [cmdInput]);

  const leaveEdit = useCallback((status = "") => {
    setMode("list");
    setEditFieldIdx(0);
    setEditId("");
    setEditForm({ name: "", base_url: "", api_key: "", model_id: "", protocol: "openai_chat", context_window: "128000", max_output_tokens: "4096" });
    setSuggestionIdx(0);
    cmdInput.setValue("");
    setStatusMsg(status);
  }, [cmdInput]);

  const commitTextField = useCallback((fieldIndex: number) => {
    const field = FIELDS[fieldIndex];
    if (field !== "protocol" && field !== "context_window") {
      setEditForm(current => ({...current, [field]: cmdInput.value}));
    }
  }, [cmdInput.value]);

  const moveEditField = useCallback((nextIndex: number) => {
    commitTextField(editFieldIdx);
    const bounded = Math.max(0, Math.min(FIELDS.length - 1, nextIndex));
    setEditFieldIdx(bounded);
    const field = FIELDS[bounded];
    if (field !== "protocol" && field !== "context_window") {
      cmdInput.setValue(String(editForm[field] || ""));
    } else {
      cmdInput.setValue("");
    }
  }, [commitTextField, editFieldIdx, editForm, cmdInput]);

  const saveEdit = useCallback(async (form: EditForm = editForm) => {
    const p: LLMProvider = {
      id: editId, name: form.name, base_url: form.base_url,
      api_key: form.api_key, model_id: form.model_id, created_at: "",
      protocol: form.protocol, capabilities: {},
      context_window: Number(form.context_window),
      max_output_tokens: Number(form.max_output_tokens),
      api_key_present: Boolean(editId), api_key_display: "",
    };
    if (!p.name || !p.base_url || !p.model_id || (!editId && !p.api_key)
        || !Number.isInteger(p.context_window) || p.context_window < 1024
        || !Number.isInteger(p.max_output_tokens) || p.max_output_tokens < 1
        || p.max_output_tokens >= p.context_window) {
      setStatusMsg(editId
        ? "Provider fields and valid token limits required"
        : "Provider fields, API Key, and valid token limits required");
      return;
    }
    setStatusMsg("Saving...");
    const result = await saveProvider(p);
    if (result) {
      leaveEdit(editId ? "Updated" : "Created");
    } else {
      setStatusMsg("Save failed");
    }
  }, [editId, editForm, saveProvider, leaveEdit]);

  const handleSwitch = useCallback(async (providerId: string) => {
    setStatusMsg("Switching...");
    const ok = await switchProvider(providerId);
    setStatusMsg(ok ? "Switched" : "Switch failed");
  }, [switchProvider]);

  const handleDelete = useCallback(async (providerId: string) => {
    setStatusMsg("Deleting...");
    const ok = await deleteProvider(providerId);
    setStatusMsg(ok ? "Deleted" : "Delete failed");
  }, [deleteProvider]);

  const handleTest = useCallback(async (p: LLMProvider) => {
    setTestResult("Testing...");
    try {
      const result: any = await llmTest(client, p.id);
      const resp = result?.response || "";
      if (result?.ok && resp) {
        setTestResult(`Connected: ${resp.slice(0, 80)}...`);
      } else {
        setTestResult("Unexpected response");
      }
    } catch (e: any) {
      setTestResult(`Connection failed: ${e.message}`);
    }
  }, [client]);

  const runCommand = useCallback(async (cmd: string) => {
    const clean = cmd.replace(/^[:\/]\s*/, "").trim();
    // Clear the operation before opening a value editor. openEdit then owns
    // the shared input and can seed the first field without this command
    // cleanup erasing it afterwards.
    cmdInput.setValue("");
    setStatusMsg("");
    setTestResult(null);
    switch (clean) {
      case "new":
        openEdit();
        break;
      case "edit": {
        if (providersView === "detail") {
          const p = focusCol === "main" ? providers[mainSelIdx] : providers[failoverSelIdx];
          if (p) openEdit(p);
          else setStatusMsg("No provider selected");
        } else {
          const active = providers.find((p) => p.id === activeProvider);
          if (active) openEdit(active);
          else setStatusMsg("No provider to edit");
        }
        break;
      }
      case "delete": {
        if (providersView === "detail") {
          const p = focusCol === "main" ? providers[mainSelIdx] : providers[failoverSelIdx];
          if (p) handleDelete(p.id);
        } else {
          const active = providers.find((p) => p.id === activeProvider);
          if (active) handleDelete(active.id);
        }
        break;
      }
      case "test": {
        if (providersView === "detail") {
          const p = focusCol === "main" ? providers[mainSelIdx] : providers[failoverSelIdx];
          if (p) handleTest(p);
        } else {
          const active = providers.find((p) => p.id === activeProvider);
          if (active) handleTest(active);
        }
        break;
      }
      case "switch": {
        if (providersView === "detail") {
          const p = focusCol === "main" ? providers[mainSelIdx] : providers[failoverSelIdx];
          if (p) handleSwitch(p.id);
        } else {
          setStatusMsg("Enter provider detail to switch");
        }
        break;
      }
      default:
        setStatusMsg(`Unknown: ${clean}`);
    }
  }, [providersView, focusCol, mainSelIdx, failoverSelIdx, providers, activeProvider,
      openEdit, handleDelete, handleTest, handleSwitch, cmdInput]);

  const commandHandlers: CommandHandlers = {
    cmdInput,
    suggestionLabels,
    suggestionIdx,
    setSuggestionIdx,
    runCommand,
  };

  // ── Keyboard ────────────────────────────────────────────
  useInput((input: string, key: any) => {
    if (!contentFocused) return false;
    const ctx: ProvidersCtx = {
      mode,
      providersView,
      focusCol,
      cmdValue: cmdInput.value,
      suggestionCount: suggestionLabels.length,
      activeSuggestion: suggestionLabels[suggestionIdx % Math.max(1, suggestionLabels.length)] || "",
      activeSuggestionMode: suggestions[suggestionIdx % Math.max(1, suggestions.length)]?.inputMode ?? "fill",
      editFieldIdx,
    };
    for (const a of resolveProvidersKey(ctx, input, key)) {
      switch (a.type) {
        case "editCancel":
          leaveEdit();
          break;
        case "editNextField":
          moveEditField(Math.min(FIELDS.length - 1, editFieldIdx + 1));
          break;
        case "editPrevField":
          moveEditField(Math.max(0, editFieldIdx - 1));
          break;
        case "editAdvance":
          if (editFieldIdx < FIELDS.length - 1) moveEditField(editFieldIdx + 1);
          else {
            commitTextField(editFieldIdx);
            const finalForm = {...editForm, [FIELDS[editFieldIdx]]: cmdInput.value};
            setEditForm(finalForm);
            void saveEdit(finalForm);
          }
          break;
        case "editText": {
          if (activeEditField === "protocol" || activeEditField === "context_window") break;
          const operation = a.op === "backspace" ? {op: "delete_back" as const}
            : a.op === "delete" ? {op: "delete_forward" as const}
            : a.op === "left" ? {op: "move_cursor" as const, delta: -1}
            : a.op === "right" ? {op: "move_cursor" as const, delta: 1}
            : a.op === "home" ? {op: "move_to_start" as const}
            : a.op === "end" ? {op: "move_to_end" as const}
            : {op: "insert" as const, text: a.text || ""};
          applyTextOp(operation, cmdInput);
          break;
        }
        case "editChoice": {
          if (activeEditField === "protocol") {
            const index = Math.max(0, PROTOCOL_OPTIONS.indexOf(editForm.protocol));
            const next = Math.max(0, Math.min(PROTOCOL_OPTIONS.length - 1, index + a.delta));
            setEditForm(form => ({...form, protocol: PROTOCOL_OPTIONS[next]!}));
          } else if (activeEditField === "context_window") {
            const current = Number(editForm.context_window);
            const nearest = CONTEXT_PRESETS.reduce((best, value, index) => (
              Math.abs(value - current) < Math.abs(CONTEXT_PRESETS[best] - current) ? index : best
            ), 0);
            const next = Math.max(0, Math.min(CONTEXT_PRESETS.length - 1, nearest + a.delta));
            setEditForm(form => ({...form, context_window: String(CONTEXT_PRESETS[next])}));
          }
          break;
        }
        case "esc":
          if (cmdInput.value.length > 0) {
            cmdInput.setValue("");
          } else if (providersView === "detail") {
            setProvidersView("projects");
          } else {
            shell.leaveContent();
          }
          break;
        case "command":
          applyCommandAction(a.action, commandHandlers);
          break;
        case "detailLeft":
          setFocusCol("main");
          break;
        case "detailRight":
          setStatusMsg("Automatic failover is not implemented in the runtime yet");
          break;
        case "detailMainMove":
          setMainSelIdx((s) => clampSel(s + a.delta, Math.max(0, providers.length - 1)));
          break;
        case "detailMainConfirm": {
          const p = providers[mainSelIdx];
          if (p) { handleSwitch(p.id); setStatusMsg(`Switched to ${p.name}`); }
          break;
        }
        case "detailFoMove":
          setStatusMsg("Automatic failover is not implemented in the runtime yet");
          break;
        case "detailFoConfirm":
          setStatusMsg("Automatic failover is not implemented in the runtime yet");
          break;
        case "detailOpen":
          setProvidersView("detail");
          setMainSelIdx(Math.max(0, providers.findIndex((p) => p.id === activeProvider)));
          setFailoverSelIdx(Math.max(0, providers.findIndex((p) => p.id !== activeProvider)));
          break;
        case "tabPrev":
          shell.tabPrev();
          break;
        case "tabNext":
          shell.tabNext();
          break;
      }
    }
  });

  // ── Render ──────────────────────────────────────────────
  if (mode === "edit") return renderEdit();
  if (providersView === "detail") return renderDualColumn();
  return renderProjectsList();

  function renderEdit() {
    return (
      <Box flexDirection="column">
        <Box paddingLeft={1}>
          <Text bold>{editId ? "Edit Provider" : "New Provider"}</Text>
          <Text dimColor>    Esc Cancel</Text>
        </Box>
        {FIELD_LABELS.map((label, i: number) => {
          const field = FIELDS[i];
          const displayedValue = i === editFieldIdx && field !== "protocol" && field !== "context_window"
            ? cmdInput.value : editForm[field];
          return (
            <Box key={label} flexDirection="row" paddingLeft={1}>
              {(() => {
                const editStyle = i === editFieldIdx ? focusedEditStyle : unfocusedEditStyle;
                return <Text color={editStyle.fg} bold={editStyle.bold}
                  dimColor={i !== editFieldIdx}>
                  {label.padEnd(10)}: {" "}
                </Text>;
              })()}
              {(() => {
                const editStyle = i === editFieldIdx ? focusedEditStyle : unfocusedEditStyle;
                return <Text color={editStyle.fg} bold={editStyle.bold}
                  dimColor={i !== editFieldIdx}>
                    {field === "api_key"
                      ? displayedValue
                        ? "•".repeat(Math.min(24, displayedValue.length))
                        : editId ? "••••••••  leave blank to retain" : ""
                      : displayedValue}
                  </Text>;
              })()}
            </Box>
          );
        })}
        <Box paddingLeft={1}>
          <Text dimColor>{chordLabel("enter")} Next / save on final field  {chordLabel("tab")} Next  {chordLabel("shiftTab")} Previous</Text>
        </Box>
        <Box paddingLeft={1}>
          <Text dimColor>Context: ←/→ 256K · 512K · 1M · 2M</Text>
        </Box>
        {statusMsg ? (
          <Box paddingLeft={1}><Text color={colors.warning}>{statusMsg}</Text></Box>
        ) : null}
      </Box>
    );
  }

  function renderProjectsList() {
    if (loading) return <Text dimColor>Loading...</Text>;
    if (providers.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No LLM providers configured.</Text>
          <Text dimColor>Type /new to create one:</Text>
          <Box paddingLeft={2} marginTop={1}>
            <Text dimColor>Name: Groq Llama 3.3</Text>
            <Text dimColor>URL:  https://api.groq.com/openai/v1</Text>
            <Text dimColor>Key:  gsk_your_key_here</Text>
            <Text dimColor>Model: llama-3.3-70b-versatile</Text>
          </Box>
        </Box>
      );
    }

    const activeP = providers.find((p) => p.id === activeProvider);
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">
          <Box flexDirection="row" flexGrow={1}>
            <Text>Default:  </Text>
            <Text
              bold={defaultRowStyle.bold}
              color={defaultRowStyle.fg}
            >
              {activeP ? `● ${activeP.name}` : "● none"}
            </Text>
          </Box>
          <Text dimColor>automatic failover unavailable</Text>
        </Box>

        <Box marginTop={1}>
          <Text dimColor>{chordLabel("enter")} models    {chordLabel("escape")} back</Text>
        </Box>
      </Box>
    );
  }

  function renderDualColumn() {
    if (loading || providers.length === 0) {
      return <Text dimColor>No providers configured.</Text>;
    }
    return (
      <Box flexDirection="column" flexGrow={1}>
        <Box marginBottom={1}>
          <Text dimColor>{chordLabel("escape")} back</Text>
        </Box>

        <Box flexDirection="row">
          <Box flexGrow={1} marginRight={1}>
            <Text dimColor bold>Main Model:</Text>
          </Box>
          <Box flexGrow={1}>
            <Text dimColor bold>Automatic Failover:</Text>
          </Box>
        </Box>

        <Box flexDirection="row" flexGrow={1}>
          <Box flexDirection="column" flexGrow={1} marginRight={1}>
            {providers.map((p, i) => {
              const isActive = p.id === activeProvider;
              const active = focusCol === "main" && i === mainSelIdx;
              return (
                <Box key={p.id} flexDirection="row">
                  <Text color={isActive ? colors.success : undefined} dimColor={!isActive}>
                    {isActive ? "●" : "○"}
                  </Text>
                  <Text
                    color={active ? colors.selection.row.fg : undefined}
                    bold={active}
                  >
                    {" "}{truncate(p.name, 40)}
                  </Text>
                </Box>
              );
            })}
          </Box>

          <Box flexDirection="column" flexGrow={1}>
            <Text color={colors.warning}>Not implemented</Text>
            <Text dimColor>Provider CRUD and manual active switch are available.</Text>
            <Text dimColor>Circuit state and automatic order will appear here after runtime support.</Text>
          </Box>
        </Box>
      </Box>
    );
  }
});
