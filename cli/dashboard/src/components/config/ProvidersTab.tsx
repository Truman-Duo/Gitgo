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

const FIELDS = ["name", "kind", "auth_mode", "base_url", "api_key", "model_id", "protocol", "context_window", "max_output_tokens"] as const;
const FIELD_LABELS = ["Name", "Location", "Auth", "Base URL", "API Key", "Model ID", "Protocol", "Context", "Max Output"] as const;
const CONTEXT_PRESETS = [8192, 32768, 64000, 128000, 256_000, 512_000, 1_000_000, 2_000_000] as const;
type EditForm = { name: string; kind: "remote" | "local"; auth_mode: "api_key" | "none"; base_url: string; api_key: string; model_id: string; protocol: NonNullable<LLMProvider["protocol"]>; context_window: string; max_output_tokens: string };
const emptyForm = (): EditForm => ({name: "", kind: "remote", auth_mode: "api_key", base_url: "", api_key: "", model_id: "", protocol: "openai_chat", context_window: "128000", max_output_tokens: "4096"});
const isChoiceField = (field: string) => ["protocol", "context_window", "kind", "auth_mode"].includes(field);

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
    if (isChoiceField(field) && matchChord("left", input, key)) return [{ type: "editChoice", delta: -1 }];
    if (isChoiceField(field) && matchChord("right", input, key)) return [{ type: "editChoice", delta: 1 }];
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
  client, project, cmdInput, onFooter, onStatusUpdate, report, shell, contentFocused,
}: ConfigTabProps) {
  const {
    providers, activeProvider, failoverEnabled, failoverOrder, configureFailover, error, health,
    loading, saveProvider, switchProvider, deleteProvider, fetchStatus,
  } = useLLMConfig(client, project);

  const [mode, setMode] = useState<Mode>("list");
  const [providersView, setProvidersView] = useState<ProvidersView>("projects");
  const [focusCol, setFocusCol] = useState<FocusCol>("main");
  const [mainSelIdx, setMainSelIdx] = useState(0);
  const [failoverSelIdx, setFailoverSelIdx] = useState(0);
  const [routingEdit, setRoutingEdit] = useState<{providerId?: string; value: number} | null>(null);
  const [routingSaving, setRoutingSaving] = useState(false);

  const [editForm, setEditForm] = useState<EditForm>(emptyForm);
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
  const backupCandidates = useMemo(() => {
    const selected = failoverOrder.map(id => providers.find(p => p.id === id)).filter((p): p is LLMProvider => Boolean(p) && p!.id !== activeProvider);
    return [...selected, ...providers.filter(p => p.id !== activeProvider && !failoverOrder.includes(p.id))];
  }, [providers, failoverOrder, activeProvider]);
  const selectedProvider = focusCol === "main" ? providers[mainSelIdx] : backupCandidates[failoverSelIdx];

  const updateFailover = useCallback(async (enabled: boolean, order: string[]) => {
    setStatusMsg("Saving failover...");
    const ok = await configureFailover(enabled, order);
    setStatusMsg(ok ? "Failover saved; new requests use this configuration" : "Failover save failed");
    return ok;
  }, [configureFailover]);

  const suggestions = useMemo<Suggestion[]>(() => {
    if (!cmdInput.value.startsWith("/")) return [];
    const prefix = cmdInput.value.slice(1).toLowerCase();
    const commands: Suggestion[] = [
      {label: "/new", description: "Create provider", inputMode: "execute"},
      {label: "/local", description: "Create local API provider", inputMode: "execute"},
      {label: "/edit", description: "Edit selected provider", inputMode: "execute"},
      {label: "/delete", description: "Delete selected provider", inputMode: "execute"},
      {label: "/test", description: "Test selected provider", inputMode: "execute"},
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
  const editUsesNormalBar = mode === "edit" && !isChoiceField(activeEditField);

  // Commands create/edit/test resources. Finite settings remain panel choices.
  useEffect(() => {
    if (!contentFocused || routingEdit || (mode === "edit" && !editUsesNormalBar)) {
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
    const statusParts = [error, statusMsg, testResult].filter(Boolean) as string[];
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
  }, [cmdInput.value, cmdInput.cursor, statusMsg, testResult, error, mode, editUsesNormalBar,
      suggestions, suggestionIdx, onFooter, cmdInput, contentFocused, routingEdit]);

  useEffect(() => {
    if (!loading && onStatusUpdate) {
      const activeName = providers.find((p) => p.id === activeProvider)?.name || "none";
      onStatusUpdate(
        `● ${activeName} active  |  ${providers.length} providers  |  failover ${failoverEnabled ? "on" : "off"} · ${failoverOrder.filter(id => id !== activeProvider).length} backups`
      );
    }
  }, [loading, providers, activeProvider, failoverEnabled, failoverOrder, onStatusUpdate]);

  const clampSel = useCallback((idx: number, max: number) => {
    return Math.max(0, Math.min(idx, Math.max(0, max)));
  }, []);

  const openEdit = useCallback((p?: LLMProvider) => {
    if (p) {
      // Secrets are never round-tripped from the backend. Empty means retain
      // the existing key; entering text explicitly replaces it.
      setEditForm({ name: p.name, kind: p.kind || "remote", auth_mode: p.auth_mode || "api_key", base_url: p.base_url, api_key: "", model_id: p.model_id, protocol: p.protocol || "openai_chat", context_window: String(p.context_window || 128000), max_output_tokens: String(p.max_output_tokens || 4096) });
      setEditId(p.id);
    } else {
      setEditForm(emptyForm());
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
    setEditForm(emptyForm());
    setSuggestionIdx(0);
    cmdInput.setValue("");
    setStatusMsg(status);
  }, [cmdInput]);

  const commitTextField = useCallback((fieldIndex: number) => {
    const field = FIELDS[fieldIndex];
    if (!isChoiceField(field)) {
      setEditForm(current => ({...current, [field]: cmdInput.value}));
    }
  }, [cmdInput.value]);

  const moveEditField = useCallback((nextIndex: number) => {
    commitTextField(editFieldIdx);
    const bounded = Math.max(0, Math.min(FIELDS.length - 1, nextIndex));
    setEditFieldIdx(bounded);
    const field = FIELDS[bounded];
    if (!isChoiceField(field)) {
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
      kind: form.kind, auth_mode: form.auth_mode,
      context_window: Number(form.context_window),
      max_output_tokens: Number(form.max_output_tokens),
      api_key_present: Boolean(editId), api_key_display: "",
    };
    if (!p.name || !p.base_url || !p.model_id || (!editId && !p.api_key && p.auth_mode !== "none")
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
      leaveEdit(result.credential_cleanup?.pending
        ? "Saved · old credential cleanup pending" : editId ? "Updated" : "Created");
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
      case "local":
        openEdit();
        setEditForm({...emptyForm(), kind: "local", auth_mode: "none", base_url: "http://127.0.0.1:11434/v1", context_window: "32768"});
        break;
      case "edit": {
        if (providersView === "detail") {
          const p = selectedProvider;
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
          const p = selectedProvider;
          if (p) handleDelete(p.id);
        } else {
          const active = providers.find((p) => p.id === activeProvider);
          if (active) handleDelete(active.id);
        }
        break;
      }
      case "test": {
        if (providersView === "detail") {
          const p = selectedProvider;
          if (p) handleTest(p);
        } else {
          const active = providers.find((p) => p.id === activeProvider);
          if (active) handleTest(active);
        }
        break;
      }
      case "switch": {
        if (providersView === "detail") {
          const p = selectedProvider;
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
      openEdit, handleDelete, handleTest, handleSwitch, cmdInput, selectedProvider]);

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
    if (routingSaving) return true;
    if (routingEdit) {
      if (matchChord("escape", input, key)) setRoutingEdit(null);
      else if (matchChord("left", input, key) || matchChord("right", input, key)) {
        const maximum = routingEdit.providerId
          ? failoverOrder.filter(id => id !== activeProvider && id !== routingEdit.providerId).length + 1 : 1;
        const delta = matchChord("right", input, key) ? 1 : -1;
        setRoutingEdit({...routingEdit, value: Math.max(0, Math.min(maximum, routingEdit.value + delta))});
      } else if (matchChord("enter", input, key)) {
        const edit = routingEdit;
        const order = edit.providerId ? failoverOrder.filter(id => id !== activeProvider && id !== edit.providerId) : [...failoverOrder];
        if (edit.providerId && edit.value > 0) order.splice(edit.value - 1, 0, edit.providerId);
        setRoutingSaving(true);
        void updateFailover(edit.providerId ? failoverEnabled : Boolean(edit.value), order).finally(() => {
          setRoutingSaving(false); setRoutingEdit(null);
          if (edit.providerId) setFailoverSelIdx(edit.value > 0 ? edit.value - 1 : order.length);
        });
      }
      return true;
    }
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
          if (isChoiceField(activeEditField)) break;
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
          if (activeEditField === "kind") {
            setEditForm(form => ({...form, kind: a.delta > 0 ? "local" : "remote", auth_mode: a.delta < 0 ? "api_key" : form.auth_mode}));
          } else if (activeEditField === "auth_mode") {
            setEditForm(form => ({...form, auth_mode: form.kind === "local" && a.delta > 0 ? "none" : "api_key"}));
          } else if (activeEditField === "protocol") {
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
          setFocusCol("failover");
          setFailoverSelIdx(-1);
          break;
        case "detailMainMove":
          setMainSelIdx((s) => clampSel(s + a.delta, Math.max(0, providers.length - 1)));
          break;
        case "detailMainConfirm": {
          const p = providers[mainSelIdx];
          if (p) void handleSwitch(p.id);
          break;
        }
        case "detailFoMove":
          setFailoverSelIdx(s => Math.max(-1, Math.min(backupCandidates.length - 1, s + a.delta)));
          break;
        case "detailFoConfirm":
          if (failoverSelIdx === -1) setRoutingEdit({value: Number(failoverEnabled)});
          else {
            const p = backupCandidates[failoverSelIdx];
            if (p) setRoutingEdit({providerId: p.id, value: failoverOrder.filter(id => id !== activeProvider).indexOf(p.id) + 1});
          }
          break;
        case "detailOpen":
          setProvidersView("detail");
          setMainSelIdx(Math.max(0, providers.findIndex((p) => p.id === activeProvider)));
          setFailoverSelIdx(-1);
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
          const displayedValue = i === editFieldIdx && !isChoiceField(field)
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
                        : editForm.auth_mode === "none" ? "not required" : editId ? "••••••••  leave blank to retain" : ""
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
          <Text dimColor>Location, auth, protocol and context: ←/→ · local endpoints must be loopback</Text>
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
          <Text dimColor>automatic failover {failoverEnabled ? "on" : "off"}</Text>
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
            <Text bold color={focusCol === "failover" && failoverSelIdx === -1 ? colors.selection.row.fg : undefined}
              backgroundColor={focusCol === "failover" && failoverSelIdx === -1 ? colors.selection.row.bg : undefined}>
              Automatic Failover: {routingEdit && !routingEdit.providerId ? (routingEdit.value ? "on" : "off") : failoverEnabled ? "on" : "off"}
            </Text>
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
                    backgroundColor={active ? colors.selection.row.bg : undefined}
                    bold={active}
                  >
                    {" "}{truncate(p.name, 32)} · {health[p.id]?.breaker_state || "unobserved"}
                  </Text>
                </Box>
              );
            })}
          </Box>

          <Box flexDirection="column" flexGrow={1}>
            {backupCandidates.map((p, index) => {
              const enabled = failoverOrder.includes(p.id);
              const priority = failoverOrder.filter(id => id !== activeProvider).indexOf(p.id);
              const selected = focusCol === "failover" && index === failoverSelIdx;
              return <Text key={p.id} bold={selected} color={selected ? colors.selection.row.fg : undefined} backgroundColor={selected ? colors.selection.row.bg : undefined}>
                {routingEdit?.providerId === p.id ? (routingEdit.value ? `[${routingEdit.value}]` : "[ ]") : enabled ? `[${priority + 1}]` : "[ ]"} {truncate(p.name, 24)}{p.kind === "local" ? " · local" : ""} · {health[p.id]?.breaker_state || "unobserved"}
              </Text>;
            })}
            {!backupCandidates.length ? <Text dimColor>Create another provider to select a backup.</Text> : null}
            <Text dimColor>{routingSaving ? "Saving failover…" : routingEdit
              ? `← → ${routingEdit.providerId ? "disabled / priority" : "off / on"} · Enter save · Esc cancel`
              : "↑ ↓ select · Enter change · ← → columns"}</Text>
          </Box>
        </Box>
      </Box>
    );
  }
});
