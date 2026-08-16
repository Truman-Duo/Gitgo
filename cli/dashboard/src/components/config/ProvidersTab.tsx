// src/components/config/ProvidersTab.tsx — /config Providers tab (self-contained).
// Owns: LLM provider list/edit/switch/delete/test + dual-column detail view.
// COMMAND input for /new /edit /delete /test /switch /toggle.

import React, { memo, useState, useEffect, useCallback, useMemo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { LLMProvider } from "../../hooks/useLLMConfig.js";
import { useLLMConfig } from "../../hooks/useLLMConfig.js";
import { agentChat } from "../../mcp/tools.js";
import {
  isCommandMode,
  resolveCommandKeys,
  applyCommandAction,
  type CommandAction,
  type CommandHandlers,
} from "../../input/commandInput.js";
import { matchChord, chordLabel } from "../../input/bindings.js";
import { colors, truncate, placeholderChar, useSelectionStyle } from "../../theme/index.js";
import type { ConfigTabProps } from "./types.js";

type Mode = "list" | "edit";
type ProvidersView = "projects" | "detail";
type FocusCol = "main" | "failover";

const FIELDS = ["name", "base_url", "api_key", "model_id"] as const;
const FIELD_LABELS = ["Name", "Base URL", "API Key", "Model ID"] as const;
type EditForm = { name: string; base_url: string; api_key: string; model_id: string };

type ProvidersCtx = {
  mode: Mode;
  providersView: ProvidersView;
  focusCol: FocusCol;
  cmdValue: string;
  suggestionCount: number;
};

type ProvidersAction =
  | { type: "editCancel" }
  | { type: "editNextField" }
  | { type: "editPrevField" }
  | { type: "editSave" }
  | { type: "editBackspace" }
  | { type: "editInsert"; text: string }
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
    if (matchChord("tabAny", input, key)) return [{ type: "editNextField" }];
    if (key.shiftTab) return [{ type: "editPrevField" }];
    if (matchChord("enter", input, key)) return [{ type: "editSave" }];
    if (matchChord("backspace", input, key)) return [{ type: "editBackspace" }];
    if (input && input.length >= 1 && !key.ctrl && !key.meta) {
      return [{ type: "editInsert", text: input }];
    }
    return [];
  }

  // Esc (list mode)
  if (matchChord("escape", input, key)) return [{ type: "esc" }];

  // COMMAND mode
  if (isCommandMode(ctx.cmdValue, input)) {
    return resolveCommandKeys(ctx.cmdValue, ctx.suggestionCount, input, key).map((a) => ({ type: "command", action: a }));
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

  // Tab switching via left/right
  if (matchChord("left", input, key)) return [{ type: "tabPrev" }];
  if (matchChord("right", input, key)) return [{ type: "tabNext" }];

  // Projects view — enter opens detail
  if (matchChord("enter", input, key)) return [{ type: "detailOpen" }];
  return [];
}

export const ProvidersTab = memo(function ProvidersTab({
  client, project, cmdInput, onFooter, onStatusUpdate, report, shell,
}: ConfigTabProps) {
  const {
    providers, activeProvider, failoverEnabled, failoverOrder,
    loading, saveProvider, switchProvider, deleteProvider, fetchStatus,
    toggleFailover,
  } = useLLMConfig(client);

  const [mode, setMode] = useState<Mode>("list");
  const [providersView, setProvidersView] = useState<ProvidersView>("projects");
  const [focusCol, setFocusCol] = useState<FocusCol>("main");
  const [mainSelIdx, setMainSelIdx] = useState(0);
  const [failoverSelIdx, setFailoverSelIdx] = useState(0);

  const [editForm, setEditForm] = useState<EditForm>({ name: "", base_url: "", api_key: "", model_id: "" });
  const [editFieldIdx, setEditFieldIdx] = useState(0);
  const [editId, setEditId] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  const [suggestionIdx, setSuggestionIdx] = useState(0);

  const suggestionLabels = useMemo(() => {
    if (!cmdInput.value.startsWith("/")) return [];
    const prefix = cmdInput.value.slice(1).toLowerCase();
    const cmds = ["/new", "/edit", "/delete", "/test", "/switch", "/toggle"];
    return cmds.filter((c) => c.slice(1).startsWith(prefix));
  }, [cmdInput.value]);

  useEffect(() => { fetchStatus(); }, [fetchStatus]);

  // Report sub/fullscreen to shell (tab bar detail + hide-on-edit).
  useEffect(() => {
    report({ sub: providersView === "detail", fullscreen: mode === "edit" });
  }, [report, providersView, mode]);

  // Footer: providers tab always shows the COMMAND bar.
  useEffect(() => {
    const statusParts = [statusMsg, testResult].filter(Boolean) as string[];
    onFooter({
      kind: "command",
      cmdInput,
      statusText: statusParts.length > 0
        ? statusParts.join(" | ")
        : `Type ${chordLabel("slash")} for commands`,
      suggestions: suggestionLabels,
      suggestionIdx,
      cmdResult: "",
    });
    return () => onFooter(null);
  }, [cmdInput.value, cmdInput.cursor, statusMsg, testResult,
      suggestionLabels, suggestionIdx, onFooter, cmdInput]);

  useEffect(() => {
    if (!loading && onStatusUpdate) {
      const activeName = providers.find((p) => p.id === activeProvider)?.name || "none";
      onStatusUpdate(
        `Failover: ${failoverEnabled ? "ON" : "OFF"}  |  ● ${activeName} active  |  ${providers.length} providers`
      );
    }
  }, [loading, providers, activeProvider, failoverEnabled, onStatusUpdate]);

  const clampSel = useCallback((idx: number, max: number) => {
    return Math.max(0, Math.min(idx, Math.max(0, max)));
  }, []);

  const openEdit = useCallback((p?: LLMProvider) => {
    if (p) {
      setEditForm({ name: p.name, base_url: p.base_url, api_key: p.api_key, model_id: p.model_id });
      setEditId(p.id);
    } else {
      setEditForm({ name: "", base_url: "", api_key: "", model_id: "" });
      setEditId("");
    }
    setEditFieldIdx(0);
    setMode("edit");
  }, []);

  const saveEdit = useCallback(async () => {
    const p: LLMProvider = {
      id: editId, name: editForm.name, base_url: editForm.base_url,
      api_key: editForm.api_key, model_id: editForm.model_id, created_at: "",
    };
    if (!p.name || !p.base_url || !p.model_id) {
      setStatusMsg("Name, Base URL, Model ID required");
      return;
    }
    setStatusMsg("Saving...");
    const result = await saveProvider(p);
    if (result) {
      setStatusMsg(editId ? "Updated" : "Created");
      setMode("list");
    } else {
      setStatusMsg("Save failed");
    }
  }, [editId, editForm, saveProvider]);

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
    if (!project) {
      setTestResult("Enter a project first to test connection");
      return;
    }
    setTestResult("Testing...");
    try {
      const result: any = await agentChat(client, project, "ping");
      const resp = result?.response || "";
      if (resp && !resp.startsWith("[Mock")) {
        setTestResult(`Connected: ${resp.slice(0, 80)}...`);
      } else if (resp.includes("Mock")) {
        setTestResult("LLM returned Mock (config may not be active)");
      } else {
        setTestResult("Unexpected response");
      }
    } catch (e: any) {
      setTestResult(`Connection failed: ${e.message}`);
    }
  }, [client, project]);

  const runCommand = useCallback(async (cmd: string) => {
    const clean = cmd.replace(/^[:\/]\s*/, "").trim();
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
      case "toggle":
        toggleFailover();
        setStatusMsg(`Failover: ${failoverEnabled ? "OFF" : "ON"}`);
        break;
      default:
        setStatusMsg(`Unknown: ${clean}`);
    }
    cmdInput.setValue("");
  }, [providersView, focusCol, mainSelIdx, failoverSelIdx, providers, activeProvider,
      openEdit, handleDelete, handleTest, handleSwitch, toggleFailover, failoverEnabled, cmdInput]);

  const commandHandlers: CommandHandlers = {
    cmdInput,
    suggestionLabels,
    suggestionIdx,
    setSuggestionIdx,
    runCommand,
  };

  // ── Keyboard ────────────────────────────────────────────
  useInput((input: string, key: any) => {
    const ctx: ProvidersCtx = {
      mode,
      providersView,
      focusCol,
      cmdValue: cmdInput.value,
      suggestionCount: suggestionLabels.length,
    };
    for (const a of resolveProvidersKey(ctx, input, key)) {
      switch (a.type) {
        case "editCancel":
          setMode("list");
          setStatusMsg("");
          break;
        case "editNextField":
          setEditFieldIdx((f: number) => (f + 1) % 4);
          break;
        case "editPrevField":
          setEditFieldIdx((f: number) => (f + 3) % 4);
          break;
        case "editSave":
          saveEdit();
          break;
        case "editBackspace": {
          const field = FIELDS[editFieldIdx];
          setEditForm((f: EditForm) => ({ ...f, [field]: f[field].slice(0, -1) }));
          break;
        }
        case "editInsert": {
          const field = FIELDS[editFieldIdx];
          setEditForm((f: EditForm) => ({ ...f, [field]: f[field] + a.text }));
          break;
        }
        case "esc":
          if (cmdInput.value.length > 0) {
            cmdInput.setValue("");
          } else if (providersView === "detail") {
            setProvidersView("projects");
          } else {
            shell.back();
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
          setFailoverSelIdx((s) => clampSel(s + a.delta, Math.max(0, providers.length)));
          break;
        case "detailFoConfirm":
          if (failoverSelIdx < providers.length) {
            const p = providers[failoverSelIdx];
            if (p) setStatusMsg(`Failover set to ${p.name} (backend API pending)`);
          } else {
            setStatusMsg("Failover: none (backend API pending)");
          }
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

  function failoverPriority(pid: string): string {
    const idx = failoverOrder.indexOf(pid);
    return idx >= 0 ? `P${idx + 1}` : "—";
  }

  function renderEdit() {
    return (
      <Box flexDirection="column">
        <Box paddingLeft={1}>
          <Text bold>{editId ? "Edit Provider" : "New Provider"}</Text>
          <Text dimColor>    Esc Cancel</Text>
        </Box>
        {FIELD_LABELS.map((label, i: number) => {
          const field = FIELDS[i];
          return (
            <Box key={label} flexDirection="row" paddingLeft={1}>
              <Text dimColor>{label.padEnd(10)}: </Text>
              {(() => {
                const editStyle = useSelectionStyle(i === editFieldIdx ? "focused" : "non-focused", "edit-field");
                return (
                  <Text
                    color={editStyle.fg}
                    backgroundColor={editStyle.bg}
                    bold={editStyle.bold}
                  >
                    {editForm[field]}
                  </Text>
                );
              })()}
              {i === editFieldIdx ? <Text color={colors.edit.placeholder.color}>{placeholderChar(true)}</Text> : null}
            </Box>
          );
        })}
        <Box paddingLeft={1}>
          <Text dimColor>{chordLabel("enter")} Save  {chordLabel("tab")} Next field  {chordLabel("shiftTab")} Prev field</Text>
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
    const defaultFO = activeP ? (failoverOrder.find((id) => id !== activeProvider) || "none") : "none";
    const defaultFOName = defaultFO !== "none" ? (providers.find((p) => p.id === defaultFO)?.name || defaultFO) : "none";
    const defaultFP = failoverPriority(activeP?.id || "");
    const defaultRowSel = useSelectionStyle("focused", "row");

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" marginBottom={1} backgroundColor={defaultRowSel.bg}>
          <Box flexDirection="row" flexGrow={1}>
            <Text backgroundColor={defaultRowSel.bg}>Default:  </Text>
            <Text
              bold={defaultRowSel.bold}
              color={defaultRowSel.fg}
              backgroundColor={defaultRowSel.bg}
            >
              {activeP ? `● ${activeP.name} (Failover: ${defaultFOName})` : "● none"}
            </Text>
          </Box>
          <Text backgroundColor={defaultRowSel.bg}>{defaultFP}</Text>
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
    const foNames = [...providers.map((p) => p.name), "none"];

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
            <Text dimColor bold>Failover:</Text>
          </Box>
        </Box>

        <Box flexDirection="row" flexGrow={1}>
          <Box flexDirection="column" flexGrow={1} marginRight={1}>
            {providers.map((p, i) => {
              const isActive = p.id === activeProvider;
              const active = focusCol === "main" && i === mainSelIdx;
              const colBg = focusCol === "main"
                ? colors.selection.block.silver.bg
                : colors.selection.dim.block.alt;
              const silverStyle = useSelectionStyle(active ? "focused" : "non-focused", "block", "silver");
              return (
                <Box key={p.id} flexDirection="row">
                  <Text color={isActive ? colors.success : "dimColor"}>
                    {isActive ? "●" : "○"}
                  </Text>
                  <Text
                    color={silverStyle.fg}
                    backgroundColor={active ? colBg : undefined}
                    bold={silverStyle.bold}
                  >
                    {" "}{truncate(p.name, 40)}
                  </Text>
                </Box>
              );
            })}
          </Box>

          <Box flexDirection="column" flexGrow={1}>
            {foNames.map((name, i) => {
              const active = focusCol === "failover" && i === failoverSelIdx;
              const colBg = focusCol === "failover"
                ? colors.selection.block.silver.bg
                : colors.selection.dim.block.alt;
              const foStyle = useSelectionStyle(active ? "focused" : "non-focused", "block", "silver");
              return (
                <Box key={name} flexDirection="row">
                  <Text color={name !== "none" ? colors.warning : "dimColor"}>
                    {name !== "none" ? "○" : "—"}
                  </Text>
                  <Text
                    color={foStyle.fg}
                    backgroundColor={active ? colBg : undefined}
                    bold={foStyle.bold}
                  >
                    {" "}{truncate(name, 40)}
                  </Text>
                </Box>
              );
            })}
          </Box>
        </Box>
      </Box>
    );
  }
});
