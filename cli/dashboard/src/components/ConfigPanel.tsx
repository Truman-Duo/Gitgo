// src/components/ConfigPanel.tsx — hierarchical providers config (single panel)
// Providers tab: Layer1=default provider summary, Layer2=dual-column model select
// Independent COMMAND input per panel. Silver-gray color-block selection.
import React, { memo, useState, useEffect, useCallback, useMemo } from "react";
import { Box, Text, useInput } from "@anthropic/ink";
import type { McpClient } from "../mcp/client.js";
import { agentChat, configGet, configSet, templateList, templateAdd, templateEdit, templateDelete, listArchivedProjects, archiveProject, deleteProject, cancelPendingDelete } from "../mcp/tools.js";
import { useLLMConfig, type LLMProvider } from "../hooks/useLLMConfig.js";
import { applyTextOp, type UseTextInputReturn } from "../hooks/useTextInput.js";
import { resolveConfigKey } from "../input/overlayKeymaps.js";
import type { FooterConfig } from "./CommandBar.js";
import { colors, usePanelSize, truncate, placeholderChar, separator, useSelectionStyle, sortByName } from "../theme/index.js";
import { chordLabel } from "../input/bindings.js";

type Props = {
  client: McpClient;
  project: string;
  cols: number;
  rows: number;
  initialTab?: string;
  cmdInput: UseTextInputReturn;
  onFooter: (cfg: FooterConfig | null) => void;
  onBack: () => void;
  onStatusUpdate?: (text: string) => void;
  onRefresh?: () => void;
};

type Mode = "list" | "edit";
type ProvidersView = "projects" | "detail";
type FocusCol = "main" | "failover";
type Tab = "providers" | "bin" | "publish";
type BinView = "menu" | "archived" | "delay";
type PublishView = "menu" | "templates" | "push";
type ArchivedProject = { name: string; workspace: string; release_url: string; pending_hard_delete_at: string };
type BinConfirm = { type: "restore" | "soft_delete" | "hard_delete"; name: string } | null;

const DELETE_DELAY_OPTIONS = [
  { label: "Immediate", value: 0 },
  { label: "1 hour", value: 60 },
  { label: "1 day", value: 1440 },
  { label: "3 days", value: 4320 },
];

const BIN_MENU: { id: Exclude<BinView, "menu">; label: string }[] = [
  { id: "archived", label: "Archived Projects" },
  { id: "delay", label: "Delete Delay" },
];

const PUBLISH_MENU: { id: Exclude<PublishView, "menu">; label: string }[] = [
  { id: "templates", label: "Templates" },
  { id: "push", label: "Push" },
];

const TABS: { id: Tab; label: string }[] = [
  { id: "providers", label: "Providers" },
  { id: "bin", label: "Bin" },
  { id: "publish", label: "Publish" },
];

export const ConfigPanel = memo(function ConfigPanel({
  client, project, cols, rows, initialTab, cmdInput, onFooter, onBack, onStatusUpdate, onRefresh,
}: Props) {
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

  // Edit form state
  const [editForm, setEditForm] = useState({ name: "", base_url: "", api_key: "", model_id: "" });
  const [editFieldIdx, setEditFieldIdx] = useState(0);
  const [editId, setEditId] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  // Suggestion index for the shared footer (managed locally, pushed via onFooter)
  const [suggestionIdx, setSuggestionIdx] = useState(0);

  // Tab state
  const [tab, setTab] = useState<Tab>((initialTab as Tab) || "providers");

  // Bin state — delete delay
  const [deleteDelayIdx, setDeleteDelayIdx] = useState(2);

  // Publish: templates
  const [templates, setTemplates] = useState<{ name: string; description: string }[]>([]);
  const [templateIdx, setTemplateIdx] = useState(0);
  const [templateEditMode, setTemplateEditMode] = useState<"idle" | "add" | "edit">("idle");
  const [templateEditName, setTemplateEditName] = useState("");
  const [templateEditDesc, setTemplateEditDesc] = useState("");
  const [templateEditField, setTemplateEditField] = useState<"name" | "desc">("name");

  // Publish: push
  const [privacyOn, setPrivacyOn] = useState(true);
  const [pushFieldIdx, setPushFieldIdx] = useState(0);
  const [publishView, setPublishView] = useState<PublishView>("menu");
  const [publishMenuIdx, setPublishMenuIdx] = useState(0);

  // Bin: archived projects + delete delay
  const [binView, setBinView] = useState<BinView>("menu");
  const [binMenuIdx, setBinMenuIdx] = useState(0);
  const [archived, setArchived] = useState<ArchivedProject[]>([]);
  const [archivedIdx, setArchivedIdx] = useState(0);
  const [archivedLoading, setArchivedLoading] = useState(true);
  const [binConfirm, setBinConfirm] = useState<BinConfirm>(null);
  const [binConfirmSel, setBinConfirmSel] = useState(1);

  // Suggestion labels — computed before footer effect (TDZ safety)
  const suggestionLabels = useMemo(() => {
    if (!cmdInput.value.startsWith("/")) return [];
    const prefix = cmdInput.value.slice(1).toLowerCase();
    if (tab === "bin" && binView === "archived") {
      const cmds = ["/restore", "/soft-delete", "/hard-delete", "/cancel-delete"];
      return cmds.filter((c) => c.slice(1).startsWith(prefix));
    }
    if (tab !== "providers") return [];
    const cmds = ["/new", "/edit", "/delete", "/test", "/switch", "/toggle"];
    return cmds.filter((c) => c.slice(1).startsWith(prefix));
  }, [cmdInput.value, tab, binView]);

  useEffect(() => { fetchStatus(); }, [fetchStatus]);

  const refreshTemplates = useCallback(() => {
    templateList(client).then((r: any) => {
      if (Array.isArray(r)) setTemplates(sortByName(r));
    }).catch(() => {});
  }, [client]);

  useEffect(() => {
    refreshTemplates();
    configGet(client).then((r: any) => {
      if (r?.safety?.delete_delay_minutes !== undefined) {
        const val = r.safety.delete_delay_minutes;
        const idx = DELETE_DELAY_OPTIONS.findIndex((o) => o.value === val);
        if (idx >= 0) setDeleteDelayIdx(idx);
      }
    }).catch(() => {});
  }, [client, refreshTemplates]);

  const refreshArchived = useCallback(async () => {
    const r: any = await listArchivedProjects(client);
    setArchived(r || []);
  }, [client]);

  useEffect(() => {
    listArchivedProjects(client).then((r: any) => {
      setArchived(r || []);
      setArchivedLoading(false);
    }).catch(() => setArchivedLoading(false));
  }, [client]);

  // Push footer config to shared CommandBar — single switch derived from nav state.
  useEffect(() => {
    const barOn = tab === "providers" || (tab === "bin" && binView === "archived");
    if (!barOn) {
      onFooter({ hidden: true });
      return () => onFooter(null);
    }
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
      suggestionLabels, suggestionIdx, onFooter, tab, binView]);

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

  // Run a command from the input
  const runCommand = useCallback(async (cmd: string) => {
    const clean = cmd.replace(/^[:\/]\s*/, "").trim();
    setStatusMsg("");
    setTestResult(null);

    if (tab === "bin" && binView === "archived") {
      const p = archived[archivedIdx];
      if (!p) {
        setStatusMsg("No archived project selected");
      } else if (clean === "restore") {
        setBinConfirm({ type: "restore", name: p.name });
        setBinConfirmSel(0);
      } else if (clean === "soft-delete") {
        setBinConfirm({ type: "soft_delete", name: p.name });
        setBinConfirmSel(0);
      } else if (clean === "hard-delete") {
        setBinConfirm({ type: "hard_delete", name: p.name });
        setBinConfirmSel(0);
      } else if (clean === "cancel-delete") {
        cancelPendingDelete(client, p.name).then(() => { refreshArchived(); onRefresh?.(); });
      } else {
        setStatusMsg(`Unknown: ${clean}`);
      }
      cmdInput.setValue("");
      return;
    }

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
      openEdit, handleDelete, handleTest, handleSwitch, toggleFailover, failoverEnabled, cmdInput,
      tab, binView, archived, archivedIdx, refreshArchived, onRefresh, client]);

  // ── Keyboard ────────────────────────────────────────────
  useInput((input: string, key: any) => {
    const ctx = {
      mode,
      tab,
      publishView,
      templateEditMode,
      binView,
      binConfirm: binConfirm !== null,
      cmdValue: cmdInput.value,
      providersView,
      focusCol,
      suggestionCount: suggestionLabels.length,
    };
    for (const a of resolveConfigKey(ctx, input, key)) {
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
          const field = ["name", "base_url", "api_key", "model_id"][editFieldIdx] as keyof typeof editForm;
          setEditForm((f: typeof editForm) => ({ ...f, [field]: f[field].slice(0, -1) }));
          break;
        }
        case "editInsert": {
          const field = ["name", "base_url", "api_key", "model_id"][editFieldIdx] as keyof typeof editForm;
          setEditForm((f: typeof editForm) => ({ ...f, [field]: f[field] + a.text }));
          break;
        }
        case "listEsc":
          if (cmdInput.value.length > 0) {
            cmdInput.setValue("");
          } else if (tab === "bin" && binView !== "menu") {
            setBinView("menu");
          } else if (tab === "publish" && publishView !== "menu") {
            setPublishView("menu");
          } else if (tab !== "providers") {
            setTab("providers");
          } else if (providersView === "detail") {
            setProvidersView("projects");
          } else {
            onBack();
          }
          break;
        case "cmdInsertSlash":
          cmdInput.insert("/");
          break;
        case "cmdRun": {
          const cmd = cmdInput.value.trim();
          if (cmd) { runCommand(cmd); setSuggestionIdx(0); }
          break;
        }
        case "cmdSuggestionUp":
          setSuggestionIdx((s) => (s - 1 + suggestionLabels.length) % suggestionLabels.length);
          break;
        case "cmdSuggestionDown":
          setSuggestionIdx((s) => (s + 1) % suggestionLabels.length);
          break;
        case "cmdSuggestionTab":
          cmdInput.setValue(suggestionLabels[suggestionIdx % suggestionLabels.length]);
          break;
        case "cmdText":
          applyTextOp(a.op, cmdInput);
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
          setTab((t) => {
            const i = TABS.findIndex((x) => x.id === t);
            return TABS[(i + TABS.length - 1) % TABS.length].id;
          });
          break;
        case "tabNext":
          setTab((t) => {
            const i = TABS.findIndex((x) => x.id === t);
            return TABS[(i + 1) % TABS.length].id;
          });
          break;
        case "binDelayAdjust":
          setDeleteDelayIdx((s) => {
            const next = Math.max(0, Math.min(DELETE_DELAY_OPTIONS.length - 1, s + a.delta));
            const val = DELETE_DELAY_OPTIONS[next]?.value;
            if (val !== undefined) configSet(client, "safety.delete_delay_minutes", val).catch(() => {});
            return next;
          });
          break;
        case "tplMove":
          setTemplateIdx((s) => Math.max(0, Math.min(templates.length - 1, s + a.delta)));
          break;
        case "tplNew":
          setTemplateEditMode("add");
          setTemplateEditName("");
          setTemplateEditDesc("");
          setTemplateEditField("name");
          break;
        case "tplEdit": {
          const t = templates[templateIdx];
          if (t) {
            setTemplateEditMode("edit");
            setTemplateEditName(t.name);
            setTemplateEditDesc(t.description || "");
            setTemplateEditField("name");
          }
          break;
        }
        case "tplDelete": {
          const t = templates[templateIdx];
          if (t && t.name !== "default") {
            templateDelete(client, t.name)
              .then(() => {
                setTemplateIdx((s) => Math.min(s, templates.length - 2));
                refreshTemplates();
              }).catch(() => {});
          }
          break;
        }
        case "tplEditSave": {
          const n = templateEditName.trim();
          if (!n) break;
          const d = templateEditDesc.trim();
          if (templateEditMode === "add") {
            templateAdd(client, { name: n, description: d, header_format: "", body_format: "" })
              .then(() => refreshTemplates()).catch(() => {});
          } else {
            templateEdit(client, n, d)
              .then(() => refreshTemplates()).catch(() => {});
          }
          setTemplateEditMode("idle");
          break;
        }
        case "tplEditBackspace":
          if (templateEditField === "name") setTemplateEditName((s) => s.slice(0, -1));
          else setTemplateEditDesc((s) => s.slice(0, -1));
          break;
        case "tplEditInsert":
          if (templateEditField === "name") setTemplateEditName((s) => s + a.text);
          else setTemplateEditDesc((s) => s + a.text);
          break;
        case "tplEditCancel":
          setTemplateEditMode("idle");
          break;
        case "tplEditSwitchField":
          setTemplateEditField((f) => (f === "name" ? "desc" : "name"));
          break;
        case "pushMove":
          setPushFieldIdx((s) => Math.max(0, Math.min(2, s + a.delta)));
          break;
        case "pushToggle":
          if (pushFieldIdx === 0) {
            setPrivacyOn((p) => {
              const next = !p;
              configSet(client, "publish.privacy_clean", next).catch(() => {});
              return next;
            });
          }
          break;
        case "binMenuMove":
          setBinMenuIdx((s) => (s + a.delta + BIN_MENU.length) % BIN_MENU.length);
          break;
        case "binMenuConfirm": {
          const item = BIN_MENU[binMenuIdx];
          if (item) setBinView(item.id);
          break;
        }
        case "publishMenuMove":
          setPublishMenuIdx((s) => (s + a.delta + PUBLISH_MENU.length) % PUBLISH_MENU.length);
          break;
        case "publishMenuConfirm": {
          const item = PUBLISH_MENU[publishMenuIdx];
          if (item) setPublishView(item.id);
          break;
        }
        case "binArchMove":
          setArchivedIdx((s) => Math.max(0, Math.min(archived.length - 1, s + a.delta)));
          break;
        case "binConfirmCancel":
          setBinConfirm(null);
          setBinConfirmSel(1);
          break;
        case "binConfirmMove":
          setBinConfirmSel(a.index);
          break;
        case "binConfirmYes": {
          if (binConfirmSel === 0 && binConfirm) {
            const { type, name } = binConfirm;
            if (type === "restore") archiveProject(client, name).then(() => { refreshArchived(); onRefresh?.(); });
            else if (type === "soft_delete") deleteProject(client, name, "soft").then(() => { refreshArchived(); onRefresh?.(); });
            else if (type === "hard_delete") deleteProject(client, name, "hard").then(() => { refreshArchived(); onRefresh?.(); });
          }
          setBinConfirm(null);
          setBinConfirmSel(1);
          break;
        }
      }
    }
  });

  const { w } = usePanelSize({ minWidth: 40, widthOffset: 4 });

  // ── Helpers ─────────────────────────────────────────────
  const failoverPriority = (pid: string): string => {
    const idx = failoverOrder.indexOf(pid);
    return idx >= 0 ? `P${idx + 1}` : "—";
  };

  // ── Render: Edit mode ────────────────────────────────────
  if (mode === "edit") {
    return (
      <Box flexDirection="column" paddingTop={1} paddingLeft={1}>
        <Box paddingLeft={1}>
          <Text bold>{editId ? "Edit Provider" : "New Provider"}</Text>
          <Text dimColor>    Esc Cancel</Text>
        </Box>
        {["Name", "Base URL", "API Key", "Model ID"].map((label, i: number) => {
          const field = ["name", "base_url", "api_key", "model_id"][i] as keyof typeof editForm;
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

  // ── Render: header + content ─────────────────────────────
  return (
    <Box flexDirection="column" paddingTop={1} paddingLeft={1} flexGrow={1}>
      {/* Header */}
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-evenly">
          {TABS.map((t) => {
            const active = t.id === tab;
            const sub = active && (
              (t.id === "providers" && providersView === "detail") ||
              (t.id === "publish" && templateEditMode !== "idle") ||
              (t.id === "bin" && binConfirm !== null)
            );
            const bg = active ? (sub ? colors.tab.detail.bg : colors.tab.active.bg) : undefined;
            const fg = active ? (sub ? colors.tab.detail.fg : colors.tab.active.fg) : colors.tab.detail.fg;
            return (
              <Box
                key={t.id}
                backgroundColor={bg}
                paddingLeft={1}
                paddingRight={1}
              >
                <Text
                  color={fg}
                  backgroundColor={bg}
                  bold={active && !sub}
                >
                  {t.label}
                </Text>
              </Box>
            );
          })}
        </Box>
        <Text color={colors.divider.color}>{separator(w)}</Text>
      </Box>

      {/* Content */}
      <Box flexDirection="column" flexGrow={1}>
        {tab === "providers"
          ? (providersView === "projects" ? renderProjectsList() : renderDualColumn())
          : tab === "bin" ? renderBin() : renderPublish()}
      </Box>
    </Box>
  );

  // ── Tab renderers ─────────────────────────────────────────

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
        {/* Default Provider row */}
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

        {/* Column headers */}
        <Box flexDirection="row">
          <Box flexGrow={1} marginRight={1}>
            <Text dimColor bold>Main Model:</Text>
          </Box>
          <Box flexGrow={1}>
            <Text dimColor bold>Failover:</Text>
          </Box>
        </Box>

        {/* Dual column list */}
        <Box flexDirection="row" flexGrow={1}>
          {/* Main Model column */}
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

          {/* Failover column */}
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

  function renderBin() {
    if (binConfirm) {
      const labels: Record<string, string> = {
        restore: "restore",
        soft_delete: "soft delete (remove governance, keep files)",
        hard_delete: "hard delete (remove EVERYTHING)",
      };
      return (
        <Box flexDirection="column">
          <Box marginBottom={1}>
            <Text bold>
              Confirm {labels[binConfirm.type]}: {binConfirm.name}
            </Text>
          </Box>
          {binConfirm.type === "hard_delete" && (
            <Box marginBottom={1}>
              <Text color={colors.danger}>
                This will delete ALL project files.{"\n"}
                Deletion is delayed per Bin settings.
              </Text>
            </Box>
          )}
          <Box flexDirection="row" marginBottom={1}>
            {(() => {
              const yesStyle = useSelectionStyle(binConfirmSel === 0 ? "focused" : "non-focused", "block", "confirm-yes");
              const noStyle = useSelectionStyle(binConfirmSel === 1 ? "focused" : "non-focused", "block", "confirm-no");
              return (
                <>
                  <Text color={yesStyle.fg} backgroundColor={yesStyle.bg} bold={yesStyle.bold}>Yes</Text>
                  <Text>     </Text>
                  <Text color={noStyle.fg} backgroundColor={noStyle.bg} bold={noStyle.bold}>No</Text>
                </>
              );
            })()}
            <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} confirm    {chordLabel("escape")} cancel</Text>
          </Box>
        </Box>
      );
    }

    if (binView === "menu") {
      return (
        <Box flexDirection="column" flexGrow={1}>
          {BIN_MENU.map((item, i) => {
            const active = i === binMenuIdx;
            const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
            return (
              <Text key={item.id} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                {item.label}
              </Text>
            );
          })}
          <Box marginTop={1}>
            <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} open    {chordLabel("escape")} back</Text>
          </Box>
        </Box>
      );
    }

    if (binView === "delay") {
      return (
        <Box flexDirection="column" flexGrow={1}>
          <Box flexDirection="column" marginBottom={1}>
            <Text dimColor bold>▸ Delete Delay</Text>
            {DELETE_DELAY_OPTIONS.map((opt, i) => {
              const active = i === deleteDelayIdx;
              const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
              return (
                <Text key={opt.value} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                  {opt.label}
                </Text>
              );
            })}
          </Box>
          <Box marginTop={1}>
            <Text dimColor>{chordLabel("upDown")} adjust delay    {chordLabel("escape")} back</Text>
          </Box>
        </Box>
      );
    }

    // binView === "archived"
    return (
      <Box flexDirection="column" flexGrow={1}>
        <Box flexDirection="column">
          <Text dimColor bold>▸ Archived Projects</Text>
          {archivedLoading ? (
            <Text dimColor>Loading...</Text>
          ) : archived.length === 0 ? (
            <Text dimColor>No archived projects.</Text>
          ) : (
            archived.map((p, i) => {
              const active = i === archivedIdx;
              const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
              const pending = p.pending_hard_delete_at
                ? `  pending delete (${p.pending_hard_delete_at.slice(0, 16)})`
                : "";
              return (
                <Box key={p.name} flexDirection="column">
                  <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                    {p.name}{pending}
                  </Text>
                  {active && p.workspace ? (
                    <Box paddingLeft={2}><Text dimColor>workspace: {p.workspace}</Text></Box>
                  ) : null}
                </Box>
              );
            })
          )}
        </Box>
        <Box marginTop={1}>
          <Text dimColor>
            {chordLabel("upDown")} select    /restore /soft-delete /hard-delete /cancel-delete    {chordLabel("escape")} back
          </Text>
        </Box>
      </Box>
    );
  }

  function renderPublish() {
    if (publishView === "menu") {
      return (
        <Box flexDirection="column" flexGrow={1}>
          {PUBLISH_MENU.map((item, i) => {
            const active = i === publishMenuIdx;
            const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
            return (
              <Text key={item.id} color={style.fg} backgroundColor={style.bg} bold={style.bold}>
                {item.label}
              </Text>
            );
          })}
          <Box marginTop={1}>
            <Text dimColor>{chordLabel("upDown")} select    {chordLabel("enter")} open    {chordLabel("escape")} back</Text>
          </Box>
        </Box>
      );
    }

    if (publishView === "templates") {
      return (
        <Box flexDirection="column" flexGrow={1}>
          {/* Template edit form */}
          {templateEditMode !== "idle" && (
            <Box flexDirection="column" marginBottom={1}>
              <Box flexDirection="row">
                <Text dimColor>Name: </Text>
                {(() => {
                  const s = useSelectionStyle(templateEditField === "name" ? "focused" : "non-focused", "edit-field");
                  return (
                    <Text color={s.fg} backgroundColor={s.bg} bold={s.bold}>
                      {templateEditName}
                    </Text>
                  );
                })()}
                {templateEditField === "name" ? <Text color={colors.edit.placeholder.color}>{placeholderChar(true)}</Text> : null}
              </Box>
              <Box flexDirection="row" marginTop={1}>
                <Text dimColor>Desc: </Text>
                {(() => {
                  const s = useSelectionStyle(templateEditField === "desc" ? "focused" : "non-focused", "edit-field");
                  return (
                    <Text color={s.fg} backgroundColor={s.bg} bold={s.bold}>
                      {templateEditDesc}
                    </Text>
                  );
                })()}
                {templateEditField === "desc" ? <Text color={colors.edit.placeholder.color}>{placeholderChar(true)}</Text> : null}
              </Box>
              <Text dimColor>{chordLabel("tab")} switch field    {chordLabel("enter")} save    {chordLabel("escape")} cancel</Text>
            </Box>
          )}

          {/* Templates list */}
          {templateEditMode === "idle" && (
            <Box flexDirection="column" marginBottom={1}>
              <Text dimColor bold>▸ Templates</Text>
              {templates.length === 0 ? (
                <Text dimColor>No templates defined</Text>
              ) : (
                templates.map((t, i) => {
                  const active = i === templateIdx;
                  const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
                  return (
                    <Box key={t.name} flexDirection="row">
                      <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>{t.name}</Text>
                      {active && t.description ? <Text dimColor>  {t.description}</Text> : null}
                    </Box>
                  );
                })
              )}
              <Text dimColor>{chordLabel("letterN")} new    {chordLabel("letterE")} edit    {chordLabel("letterD")} delete    {chordLabel("escape")} back</Text>
            </Box>
          )}
        </Box>
      );
    }

    // publishView === "push"
    return (
      <Box flexDirection="column" flexGrow={1}>
        <Box flexDirection="column">
          <Text dimColor bold>▸ Push</Text>
          {([
            { key: "privacy", label: `Privacy Clean: ${privacyOn ? "ON" : "OFF"}`, value: "" },
            { key: "format", label: "Commit Format", value: "[project-prefix] brief description" },
            { key: "release", label: "Release URL", value: "configured per project in LLM section" },
          ] as const).map((row, i) => {
            const active = pushFieldIdx === i;
            const style = useSelectionStyle(active ? "focused" : "non-focused", "row");
            return (
              <Box key={row.key} flexDirection="row">
                <Text color={style.fg} backgroundColor={style.bg} bold={style.bold}>{row.label}</Text>
                {row.value ? <Text dimColor>  {row.value}</Text> : null}
              </Box>
            );
          })}
          <Text dimColor>{chordLabel("upDown")} select field    {chordLabel("enter")} toggle privacy    {chordLabel("escape")} back</Text>
        </Box>
      </Box>
    );
  }

});
